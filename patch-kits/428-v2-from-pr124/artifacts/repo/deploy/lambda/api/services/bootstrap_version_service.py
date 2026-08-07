# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#389 v2 块5 — bootstrap 版本切换 API(GET /bootstrap/versions + POST /bootstrap/promote)。

契约(grill 决策):API 只接受"切换到某个【已存在】的 bootstrap 版本"—— 传 sha256,不传脚本
内容。**API 绝不新建 LaunchTemplate 版本、绝不上传脚本**:可切换的版本集 = `cdk deploy`【已经
发布】的那些 LT 版本(每个版本的 user-data 里编了一个 bootstrap sha256)。这样即使 API key 泄露,
后果从"注入任意代码"降到"在你自己 cdk 部署过的历史版本间切默认":攻击者拿不到写 user-data 的
能力,只能把默认版本切到某个你亲手部署过的版本。授权门是 admin(复用 _get_caller_identity)。

为什么这样最安全(codex 评审确认):给在线 Lambda 授 CreateLaunchTemplateVersion 等于给它写任意
user-data/AMI 的能力(继承实例角色 → 任意代码),即便本代码只做 rekey 也无法从 IAM 层保证。故
彻底移除 Create/RunInstances/PassRole/UpdateAutoScalingGroup,API 只保留 ModifyLaunchTemplate
(翻默认版本)。两台 ASG 都跟踪 `$Default`,EC2 每次 launch 解析默认版本 → 翻默认 = 下次开机读那个
版本的 bootstrap,不碰存量在跑实例(K1)。

为什么独立成模块:handler.py / host_service.py 都已超 800 行硬上限,按 code-craft 规则
"改已超限老文件时新代码不再往里堆",新增面单独落这里(同 image_slot_service.py 的理由)。

一次 promote 的形状:
  1. **fleet 锁 + admin 门**:整段串行到一台 fleet(并发 promote 收敛),锁内重读当前态。
  2. **枚举 LT 已发布版本**:describe-launch-template-versions 翻页,每个版本 user-data 解析出
     bootstrap sha,建 {sha -> 最高版本号}。当前 = $Default 解析出的 sha。
  3. **CAS**:expected_current_sha == current;不符即拒(挡"验证 A 却切了 C")。
  4. **目标必须是已发布版本**:target_sha 不在 {已发布 sha} → 404(只能切 cdk 部署过的版本)。
     再 GetObject 目标 bootstrap S3 对象核对字节 sha(防对象被删/被改后实例开机拿不到/拿错)。
  5. **ModifyLaunchTemplate** 把默认版本翻到目标版本(不 Create、不 UpdateASG)。
  6. **read-back**:再解析 $Default 的 sha == 目标,才算成功;失败 503 UNKNOWN(不谎报)。
  7. **K1**:不触发 instance refresh;存量在跑实例随自然替换接手。
  8. **强制审计**:before→after sha + actor + 版本号 + status(含 UNKNOWN 与幂等对账 reconciled)。

真值来源是 ASG 跟踪的 LT $Default 的 user-data;从 ASG 反查 LT id(edge LT 名带 _gsuffix 也无所谓)。
"""

import base64
import hashlib
import os
import time
import uuid

import boto3

from core.bootstrap_lt import BootstrapParseError, classify, rekey, target_key
from core.clients import asg_client, audit_table, hosts_table, s3
from core.utils import _err, _now, _resp

ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")

# fleet → ASG 名。两个都是固定名(host 见 lambdas.py ASG_NAME,edge 见 edge_admin.EDGE_ASG_NAME);
# 从 ASG 反查 LT,故这里不需要 LT 名(edge LT 名带 _gsuffix,不固定)。
FLEET_ASG = {"host": "openclaw-hosts-asg", "edge": "openclaw-edge-asg"}

# promote 的 fleet 级互斥锁:一条存进 hosts_table 的合成记录(__ 前缀,同 __az_failover_state__
# 约定,_public_hosts 会滤掉、调度扫描按 status IN(active,idle) 也选不到,绝不被当 host)。
# 为什么必须锁:CAS(读 current → 校验 expected → 翻默认版本)不是原子的,两个并发 promote
# 带同一 expected、不同 target 会都过 CAS,依次翻默认,后者赢但前者已回了 200(并发不收敛)。
# 锁内【重新读一次 current】再校验,把整段 check-and-flip 串行化到一台 fleet 上。
_LOCK_KEY_PREFIX = "__bootstrap_promote_lock__"
# 锁租期必须【覆盖 api Lambda 的最大执行时长】(lambdas.py: timeout=900s),否则慢 promote 还
# 没跑完锁就过期,第二个 promote 接管锁、与第一个并发翻默认,互斥又失效(同 image_lease
# 用 1200s 的理由)。给到 1200s = 900s 上限 + 冗余。过期接管只在原持有者【确实已死】时发生
# (Lambda 被杀,不会再翻默认),故这是安全的兜底,不是活锁窗口。
_LOCK_TTL_S = 1200


def _lock_key(fleet):
    return f"{_LOCK_KEY_PREFIX}{fleet}"


def _acquire_lock(fleet):
    """抢 fleet 级 promote 锁(hosts_table 条件写)。返回 (token|None, err|None)。

    条件:锁不存在,或已过期(lease_until <= now)才能拿。过期可接管挡"持锁者中途死"。
    """
    token = f"promote-{uuid.uuid4().hex[:16]}"
    now = int(time.time())
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        hosts_table.put_item(
            Item={
                "instance_id": _lock_key(fleet),
                "status": "promote-lock",  # 非 active/idle → 调度扫描选不到
                "lock_owner": token,
                "lock_until": now + _LOCK_TTL_S,
            },
            ConditionExpression="attribute_not_exists(instance_id) OR lock_until <= :now",
            ExpressionAttributeValues={":now": now},
        )
        return token, None
    except ccf:
        return None, _err(
            409, "PROMOTE_IN_PROGRESS",
            f"another {fleet} bootstrap promote is in progress; retry shortly",
        )
    except Exception as e:  # noqa: BLE001 — DDB throttle/不可用不能逃成裸 500,收成 503 契约错误
        return None, _err(
            503, "DEPENDENCY_UNAVAILABLE",
            f"cannot acquire {fleet} promote lock (DynamoDB): {e}",
        )


def _release_lock(fleet, token):
    """归还锁(只删自己持有的那把;过期后被别人接管则条件不符,不误删对方的)。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        hosts_table.delete_item(
            Key={"instance_id": _lock_key(fleet)},
            ConditionExpression="lock_owner = :t",
            ExpressionAttributeValues={":t": token},
        )
    except ccf:
        pass  # 锁已过期被接管:不是我们的了,别删
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap-promote] WARN release lock {fleet} failed: {e}")


def _ec2():
    # 同 host_service / edge_admin:core.clients 无 ec2 单例,本地构造(region 由 runtime 提供)。
    return boto3.client("ec2")


def _asg_lt_spec(asg):
    """从 ASG 描述里取它真正 pin 的 LaunchTemplateSpecification。返回 (spec|None, override_err|None)。

    MIP 下每个 Overrides[*] 可带自己的 LaunchTemplateSpecification 覆盖基础 LT/版本 —— 若有 override
    指到别的 LT 或固定了版本,翻基础 LT 的默认版本对那部分实例无效(promote 会误报 200 但部分新实例仍用
    override 的版本/LT,codex 评审确认的 false-green)。故 MIP 有【任何】override 带 LaunchTemplateSpecification
    时 fail-closed(override_err),不返回可操作的 spec。"""
    lt = asg.get("LaunchTemplate")
    if lt:
        return lt, None
    mip = asg.get("MixedInstancesPolicy") or {}
    spec = (mip.get("LaunchTemplate") or {}).get("LaunchTemplateSpecification")
    if not spec:
        return None, None
    for ov in mip.get("Overrides") or []:
        if ov.get("LaunchTemplateSpecification"):
            return None, (
                "MixedInstancesPolicy has an override with its own LaunchTemplateSpecification; "
                "flipping the base LaunchTemplate default would not affect instances launched "
                "from the override. Refusing to promote."
            )
    return spec, None


def _resolve_lt_id(fleet):
    """从 fleet 的 ASG 反查它跟踪的 LaunchTemplate id,并【强制】确认 ASG 跟踪的是 $Default。
    返回 (lt_id|None, err|None)。describe 的 boto 异常收成契约错误(503),绝不逃到 handler 变裸 500。

    为什么必须校验 $Default:promote 靠翻 LT 默认版本生效。若 ASG 实际固定到某个数字版本或 $Latest
    (人手改过、或未来某处 override),翻默认对它无效 —— 会翻默认、回读 LT 后误报 200,但新实例仍用
    ASG 固定的版本(codex 评审确认的 false-green)。此时 fail-closed(不静默失败也不谎报)。"""
    asg_name = FLEET_ASG[fleet]
    try:
        groups = asg_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asg_name]
        ).get("AutoScalingGroups") or []
    except Exception as e:  # noqa: BLE001
        return None, _err(503, "DEPENDENCY_UNAVAILABLE",
                          f"cannot describe {fleet} ASG {asg_name}: {e}")
    if not groups:
        return None, _err(404, "FLEET_NOT_DEPLOYED",
                          f"{fleet} ASG {asg_name} not found; this fleet is not deployed")
    spec, override_err = _asg_lt_spec(groups[0])
    if override_err:
        return None, _err(409, "ASG_OVERRIDES_LAUNCH_TEMPLATE",
                          f"{fleet} ASG {asg_name}: {override_err}")
    if not spec or not spec.get("LaunchTemplateId"):
        return None, _err(409, "LT_NOT_RESOLVABLE",
                          f"{fleet} ASG {asg_name} has no resolvable LaunchTemplate id")
    tracked = spec.get("Version")
    if tracked != "$Default":
        return None, _err(
            409, "ASG_NOT_TRACKING_DEFAULT",
            f"{fleet} ASG {asg_name} tracks LaunchTemplate version {tracked!r}, not $Default; "
            f"flipping the default version would not change what it launches. Refusing to "
            f"promote (redeploy so the ASG tracks $Default, or fix the ASG version).",
        )
    return spec["LaunchTemplateId"], None


def _published_versions(ec2, lt_id, fleet):
    """枚举 LT【已发布】的所有版本。翻页到底。返回 (versions, default_rec, err):
      · versions: [{version_number, is_default, ud, ltdata}](每个能解析出 bootstrap sha 的版本;
        ud=解码后的 user-data 明文,ltdata=完整 LaunchTemplateData)。【不按 sha 折叠】—— 折叠会丢掉
        真实默认版本的 ltdata 基线,让 _switchable 拿错版本当基线(codex 评审确认)。
      · default_rec: 真实【默认版本】那条记录(含它自己的 ud/ltdata/sha);默认版本不可解析 → None。
    翻页漏读 = 漏版本/误判 default,故必须翻到底。describe 异常 → err(503)。"""
    versions = []
    default_rec = None
    token = None
    try:
        while True:
            kw = {"LaunchTemplateId": lt_id, "MaxResults": 200}
            if token:
                kw["NextToken"] = token
            resp = ec2.describe_launch_template_versions(**kw)
            for v in resp.get("LaunchTemplateVersions") or []:
                num = v.get("VersionNumber")
                is_default = bool(v.get("DefaultVersion"))
                ltdata = v.get("LaunchTemplateData") or {}
                raw = ltdata.get("UserData")
                if raw is None or num is None:
                    continue
                try:
                    ud = base64.b64decode(raw).decode("utf-8")
                    info = classify(ud, fleet)
                    sha, bkt = info["sha256"], info["bucket"]
                except Exception:  # noqa: BLE001 — 解码/解析失败:该版本不作可切目标,但不致命
                    continue  # 默认版本若不可解析 → default_rec 留 None → promote fail-closed
                rec = {"version_number": int(num), "is_default": is_default,
                       "ud": ud, "sha256": sha, "bucket": bkt, "ltdata": ltdata}
                versions.append(rec)
                if is_default:
                    default_rec = rec
            token = resp.get("NextToken")
            if not token:
                break
    except Exception as e:  # noqa: BLE001
        return None, None, _err(
            503, "DEPENDENCY_UNAVAILABLE",
            f"cannot enumerate {fleet} LaunchTemplate versions: {e}")
    return versions, default_rec, None


def _switchable(versions, default_rec, fleet):
    """从已发布版本里筛出【可切换】版本,以【真实默认版本】为基线。返回 {sha -> version_number}
    (同 sha 多可切版本留最高版本号)。default 未知 → 空(promote fail-closed)。

    可切换的判据(真正的授权/安全边界,codex 评审确认):候选版本必须
      (1) 除 UserData 外的 LaunchTemplateData 字段与默认版本逐字段相等(不改 AMI/role/网络/磁盘/IMDS);
      (2) 且它的 user-data 恰好是把默认版本 user-data 的 bootstrap 摘要换成候选自己摘要的【逐字节 rekey】
          结果 —— 这样候选 user-data 不能夹带任何"额外 shell 命令"(即便它复用了历史 S3 对象、别的字段
          也一样):rekey 是纯摘要替换,任何多出来的字节都会让 (2) 不成立。
    默认版本自身恒可切(幂等目标)。

    关于"必须是 CDK 来源"的残余(codex 评审提过):这里【不】做不可伪造的部署来源 allowlist,因为
    (1)+(2) 已经让"可切候选"= 与当前默认逐字节只差 bootstrap 摘要 的版本。一个手工建的版本若满足
    (1)+(2),它在字节上就等价于 CDK 会为该摘要产出的 rekey 版本,且它指向的 S3 对象还要过
    _verify_target_object 的三方摘要核对 —— 即攻击者即便手工建版本,也只能把机队切到"某个真实存在、
    字节自洽的 bootstrap 对象",拿不到写脚本能力,与切一个真 CDK 版本无差别。真正的写入权仍归 CDK
    /setup.sh。若未来要更强的来源绑定,可给 LT 版本打 CDK 部署 tag 再在此校验(留作加固,不在本 scope)。"""
    if default_rec is None:
        return {}
    # 默认版本的 bootstrap 必须从【本 stack 的 ASSETS_BUCKET】下载。若默认版本指向别的 bucket,整个
    # fleet 都不可切(_verify_target_object 只核对 ASSETS_BUCKET 里的对象,切到别 bucket 的版本会
    # 让实例开机拿不到 → false-green,codex 评审确认)。current 不在本桶 → 返回空,fail-closed。
    if default_rec["bucket"] != ASSETS_BUCKET:
        print(f"[bootstrap-promote] WARN {fleet} default version downloads from bucket "
              f"{default_rec['bucket']!r}, not ASSETS_BUCKET {ASSETS_BUCKET!r}; nothing switchable")
        return {}
    default_ud = default_rec["ud"]
    default_non_ud = {k: v for k, v in (default_rec["ltdata"] or {}).items() if k != "UserData"}
    out = {}
    for rec in versions:
        sha = rec["sha256"]
        if rec["is_default"]:
            out[sha] = max(out.get(sha, 0), rec["version_number"])
            continue
        # 候选也必须从 ASSETS_BUCKET 下载(rekey 只改摘要不改 bucket,故 (2) 已隐含;显式再挡一层)。
        if rec["bucket"] != ASSETS_BUCKET:
            continue
        # (1) 除 UserData 外逐字段相等
        cand_non_ud = {k: v for k, v in (rec["ltdata"] or {}).items() if k != "UserData"}
        if cand_non_ud != default_non_ud:
            continue
        # (2) user-data 必须是默认 user-data 到候选摘要的逐字节 rekey(不能夹带额外命令)
        try:
            expected_ud = rekey(default_ud, fleet, sha)
        except BootstrapParseError:
            continue
        if rec["ud"] != expected_ud:
            continue
        out[sha] = max(out.get(sha, 0), rec["version_number"])
    return out


def _verify_target_object(fleet, new_sha):
    """目标 bootstrap S3 对象必须已存在且回读字节 sha256 == new_sha(纵深:实例开机会 sha256sum -c
    这个对象,对象没了/被改则实例开机拿不到或校验失败)。返回 err|None。这【不是】授权边界(授权边界
    是"target 必须在 LT 已发布版本集里"),是防"切到一个 S3 对象已被删/被篡改的版本"的健壮性检查。"""
    key = target_key(fleet, new_sha)
    try:
        obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=key)
        body = obj["Body"].read()
    except s3.exceptions.NoSuchKey:
        return _err(
            404, "VERSION_NOT_FOUND",
            f"no bootstrap object at s3://{ASSETS_BUCKET}/{key}; the version's S3 object is "
            f"missing (was it pruned?) — cannot switch a fleet to bytes that are not there",
        )
    except Exception as e:  # noqa: BLE001 — S3 读失败一律 fail-closed,绝不带疑点继续改机队
        return _err(503, "DEPENDENCY_UNAVAILABLE", f"cannot read target bootstrap object: {e}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != new_sha:
        return _err(
            409, "DIGEST_MISMATCH",
            f"object at {key} hashes to {actual}, not the requested {new_sha}; refusing to "
            f"point the fleet at bytes that do not match their digest-addressed key",
        )
    return None


def list_versions():
    """GET /bootstrap/versions — 列 host+edge 【已发布的 LT 版本】(按 bootstrap sha),标出当前默认。

    可切换集 = cdk deploy 已发布的 LT 版本,不是 S3 桶里的对象(API 只能切到 CDK 部署过的版本)。
    """
    ec2 = _ec2()
    fleets = {}
    for fleet in FLEET_ASG:
        lt_id, err = _resolve_lt_id(fleet)
        if err:
            fleets[fleet] = {"available": [], "current_sha": None, "error": err["body"]}
            continue
        versions, default_rec, verr = _published_versions(ec2, lt_id, fleet)
        if verr:
            fleets[fleet] = {"available": [], "current_sha": None, "error": verr["body"]}
            continue
        current_sha = default_rec["sha256"] if default_rec else None
        current_ver = default_rec["version_number"] if default_rec else None
        # 只列【可切换】版本(与当前默认除 UserData 外一致 + user-data 是纯 rekey);别的不列。
        switchable = _switchable(versions, default_rec, fleet)
        available = sorted(
            ({"sha256": sha, "launch_template_version": ver, "is_current": sha == current_sha}
             for sha, ver in switchable.items()),
            key=lambda a: a["launch_template_version"], reverse=True,
        )
        fleets[fleet] = {
            "asg": FLEET_ASG[fleet], "launch_template_id": lt_id,
            "current_sha": current_sha, "current_launch_template_version": current_ver,
            "available": available,
        }
    return _resp(200, {"fleets": fleets})


def promote(fleet, body, actor):
    """POST /bootstrap/promote — 把某 fleet 的 LT 默认版本切到一个【已发布】的 bootstrap 版本。

    body: {fleet, target_sha, expected_current_sha}。actor: {owner_id, role, api_key_only}
    (handler 已在门口过 admin;这里只负责 CAS/校验/翻默认版本/审计)。
    """
    if not ASSETS_BUCKET:
        return _err(503, "NOT_CONFIGURED", "ASSETS_BUCKET is not configured")
    if fleet not in FLEET_ASG:
        return _err(400, "VALIDATION", f"fleet must be one of {sorted(FLEET_ASG)}")
    payload = body if isinstance(body, dict) else _parse(body)
    if payload is None:
        return _err(400, "VALIDATION", "body must be a JSON object")
    # 字段必须是字符串:JSON 里 target_sha:[] / fleet:1 会让 .strip() 抛 → 必须先判类型挡成 400,不 500。
    target_raw = payload.get("target_sha")
    expected_raw = payload.get("expected_current_sha")
    if target_raw is not None and not isinstance(target_raw, str):
        return _err(400, "VALIDATION", "target_sha must be a string")
    if expected_raw is not None and not isinstance(expected_raw, str):
        return _err(400, "VALIDATION", "expected_current_sha must be a string")
    target_sha = (target_raw or "").strip().lower()
    expected = (expected_raw or "").strip().lower()
    if not _is_sha(target_sha):
        return _err(400, "VALIDATION", "target_sha must be a 64-hex sha256")
    if not expected:
        return _err(
            400, "VALIDATION",
            "expected_current_sha required (CAS: proves you switch from the version you read)",
        )

    # 整段 check-and-flip 必须在 fleet 级锁内串行:CAS 非原子,并发同 expected 不同 target 会都过
    # 校验、依次翻默认(后者赢、前者已谎报 200),违背并发收敛。锁内重读 current 再判。
    token, lock_err = _acquire_lock(fleet)
    if lock_err:
        return lock_err
    try:
        return _promote_locked(fleet, target_sha, expected, actor)
    finally:
        _release_lock(fleet, token)


def _promote_locked(fleet, target_sha, expected, actor):
    """持 fleet 锁后执行:枚举已发布版本 → CAS → target 必须已发布 → 校验 S3 对象 → 翻默认 → read-back。"""
    ec2 = _ec2()
    lt_id, err = _resolve_lt_id(fleet)
    if err:
        return err
    versions, default_rec, verr = _published_versions(ec2, lt_id, fleet)
    if verr:
        return verr
    if default_rec is None:
        return _err(
            409, "CURRENT_UNPARSEABLE",
            f"cannot read the {fleet} fleet's current (default) bootstrap version; "
            f"refusing to promote from an unknown state",
        )
    current = default_rec["sha256"]

    # target 必须是【可切换版本】—— 授权边界:已发布 + 与当前默认除 bootstrap user-data 外逐字段一致
    # + user-data 是纯 rekey(不夹带额外命令)。不 Create 版本、拿不到写 user-data 能力,且顺带改了
    # AMI/role/网络或塞了额外命令的版本都不可切。不在可切集 → 404。
    switchable = _switchable(versions, default_rec, fleet)
    target_version = switchable.get(target_sha)
    if target_version is None:
        published = any(v["sha256"] == target_sha for v in versions)
        reason = (
            "it changes more than the bootstrap user-data (AMI/role/network/extra commands/…) vs "
            "the current default, so switching to it would change the fleet's launch posture"
            if published else
            "it is not a published LaunchTemplate version deployed by cdk"
        )
        return _err(
            404, "VERSION_NOT_FOUND",
            f"{target_sha} is not switchable for {fleet}: {reason}. This API never creates "
            f"versions or uploads scripts. GET /bootstrap/versions for the switchable set.",
        )

    # 纵深:目标版本对应的 S3 bootstrap 对象也必须在且字节自洽(实例开机会下载+校验它)。
    # 先于任何 success 返回(含幂等 no-op):对象没了就不能声称能切到它。
    obj_err = _verify_target_object(fleet, target_sha)
    if obj_err:
        return obj_err

    # 幂等:默认版本已是 target → no-op 成功(重放安全)。也审计(reconciled):这是"503 后重试
    # 对账"的落点——若上次 modify 服务端已生效但客户端收到异常(记了 UNKNOWN),重试到这里发现已切,
    # 必须补一条 SUCCEEDED,否则真实机队变更永久无成功审计痕迹(codex 高危 gap)。
    # 版本号用【真实默认版本】default_rec["version_number"],不是 _switchable 折叠出的最高版本 ——
    # 同 sha 有多个等价版本时,实际 $Default 可能是较低的那个,审计/回执必须记真实生效的那个(codex)。
    if current == target_sha:
        current_ver_num = default_rec["version_number"]
        if not _audit(fleet, current, target_sha, current_ver_num, actor,
                      "SUCCEEDED", reconciled=True):
            return _err(
                503, "AUDIT_UNAVAILABLE",
                f"{fleet} already boots {target_sha} but the audit record could not be "
                f"persisted; refusing to confirm a high-privilege state without an audit trail. "
                f"Retry once the audit table is writable.",
            )
        return _resp(200, {
            "message": "fleet already boots the target version", "fleet": fleet,
            "already_promoted": True, "current_sha": current,
            "current_launch_template_version": current_ver_num,
        })
    if expected != current:
        return _err(
            409, "CAS_MISMATCH",
            f"{fleet} fleet currently boots {current}, not the expected {expected}; re-read "
            f"GET /bootstrap/versions and retry (never promote from a stale view)",
        )

    # 【变更前】先落一条 INTENT 审计:记"要把 fleet 从 current 切到 target"。intent 落不下就【不】动
    # 机队 —— 保证任何真实变更之前都已有痕迹(codex 评审确认的 gap:若"改后再审计"且审计失败、锁释放、
    # 又来一轮 promote,原请求重试会 CAS 失败 → A→B 永久无审计)。intent 在变更前落,即便后续 CAS 状态
    # 变了,也永远查得到"这次尝试把 A 切到 B"。
    if not _audit(fleet, current, target_sha, target_version, actor, "INTENT"):
        return _err(
            503, "AUDIT_UNAVAILABLE",
            f"cannot persist a pre-change audit intent for {fleet}; refusing to modify the "
            f"fleet without an audit trail. Retry once the audit table is writable.",
        )

    # 翻默认版本到目标已发布版本。无论抛不抛异常都 read-back —— modify 可能服务端已生效但客户端
    # 收到异常(那时机队真变了却收到错),read-back 才是唯一权威判据。
    raised = False
    try:
        ec2.modify_launch_template(LaunchTemplateId=lt_id, DefaultVersion=str(target_version))
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap-promote] WARN modify-launch-template {lt_id} raised "
              f"(may have applied): {e}")
        raised = True

    _rb_versions, rb_default, rb_err = _published_versions(ec2, lt_id, fleet)
    rb_current = rb_default["sha256"] if rb_default else None
    rb_ver = rb_default["version_number"] if rb_default else None
    # 成功判据必须【同时】核对默认版本的 sha 和【版本号】== 我们要切到的那个。只比 sha 会被"并发把
    # 另一个同 sha 但改了 AMI/role 的版本设成默认"骗过(read-back 见到同 sha 就误报 200,codex 评审
    # 确认)。版本号也对上,才确认默认就是我们校验过、要切的那个版本。
    applied = (not rb_err) and rb_current == target_sha and rb_ver == target_version
    terminal_audited = _audit(fleet, current, target_sha, target_version, actor,
                              "SUCCEEDED" if applied else "OPERATION_STATUS_UNKNOWN")
    if applied:
        # 变更已生效。终态 SUCCEEDED 审计若也写失败(INTENT 成功但这条失败),不能报 200 —— 那样只有
        # INTENT 没有 SUCCEEDED,事后分不清这次到底成没成(codex 评审确认)。此时报 503,重试走幂等对账
        # 路径补一条 SUCCEEDED(reconciled)再 200,保证"每次 200 都对应一条已持久化的 SUCCEEDED"。
        if not terminal_audited:
            return _err(
                503, "AUDIT_UNAVAILABLE",
                f"flipped {fleet} default to {target_sha} but the terminal audit record could "
                f"not be persisted; the change is applied — retry with the same params to record "
                f"the SUCCEEDED audit (idempotent).",
            )
        return _resp(200, {
            "message": "fleet bootstrap version promoted", "fleet": fleet,
            "already_promoted": False,
            "previous_sha": current, "current_sha": target_sha,
            "launch_template_version": target_version,
            # K1:存量在跑的 host 不受影响,随自然替换接手新版本。
            "note": "existing running instances are unchanged (no instance refresh); new "
                    "launches boot the promoted version",
            "promoted_at": _now(),
        })
    detail = "modify-launch-template raised" if raised else "read-back did not confirm"
    return _err(
        503, "OPERATION_STATUS_UNKNOWN",
        f"attempted to flip {fleet} default to version {target_version} ({target_sha}) but "
        f"{detail} (saw {rb_current if not rb_err else 'read-back error'}); verify with "
        f"GET /bootstrap/versions and retry with the same params (idempotent).",
    )


def _audit(fleet, old_sha, new_sha, new_version, actor, status, reconciled=False):
    """强制审计:机队级启动路径变更必须留 before→after 痕迹(高权限操作)。返回 True=已【持久化】。

    与 handler._audit_write 的 best-effort 不同:这是高权限机队变更,审计必须落。故返回布尔,
    调用方在【成功路径】上把"审计没落"当 503 处理 —— 一次改了机队却没审计痕迹的 200 是不可接受的
    (codex 评审确认)。表缺失(未部署)同样返回 False,让成功路径 fail-closed。
    reconciled=True 表示"这条 SUCCEEDED 是重试对账时补的",便于分辨 503 后重试确认的成功。"""
    if audit_table is None:
        print(f"[bootstrap-promote] WARN audit table not configured; cannot record {fleet} promote")
        return False
    try:
        import uuid

        actor = actor or {}
        audit_table.put_item(Item={
            "pk": "audit", "id": str(uuid.uuid4()), "ts": _now(),
            "operation": f"bootstrap-promote {fleet}",
            "resource_id": fleet,
            "detail_old_sha": old_sha, "detail_new_sha": new_sha,
            "detail_new_lt_version": new_version, "detail_status": status,
            "detail_reconciled": bool(reconciled),
            "actor_owner_id": actor.get("owner_id") or "",
            "actor_role": actor.get("role") or "",
            "actor_api_key_only": bool(actor.get("api_key_only")),
            "expires_ttl": int(time.time()) + 90 * 86400,
        })
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap-promote] WARN audit write failed for {fleet}: {e}")
        return False


def _is_sha(v):
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def _parse(body):
    if body is None or body == "":
        return {}
    import json

    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
