# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import time
import boto3
from botocore.config import Config

ssm = boto3.client(
    "ssm", config=Config(retries={"max_attempts": 8, "mode": "adaptive"})
)
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
# Prefer the WORM + CMK backup bucket; fall back to assets bucket only if unset
# (so a half-deployed stack still backs up rather than crashing).
BUCKET = os.environ.get("BACKUP_BUCKET") or os.environ["ASSETS_BUCKET"]
PREFIX = os.environ.get("BACKUP_PREFIX", "backups")
CMK_KEY_ID = os.environ.get("BACKUP_CMK_KEY_ID", "")

# ── #564 G7 —— 手动备份的相位取值 ────────────────────────────────────────────
#
# **这些字面值的权威定义在 `deploy/lambda/api/services/tenant_service.py` 的
# `_BACKUP_PHASE_*`**,不在这里。本 Lambda 的 asset 只含 `handler.py` 一个文件
# (`deploy/stacks/lambdas.py` 的 `Code.from_asset("deploy/lambda/backup")`),所以
# import 不到那边 —— 只能抄一份字面值。**一致性不靠"记得同步改"**:
# `tests/test_564_g6g7_dlq_backup.py` 有一条断言把两处逐值比对,漂了就红。
# 同样的处置在 `deadline_executor._REBUILD_INFLIGHT_PHASES` 上已经用过一次。
_PHASE_QUEUED = "queued"      # 生产端受理时写的初始相位;起跑的来源相位锚要用它
_PHASE_RUNNING = "running"
_PHASE_SUCCEEDED = "succeeded"
_PHASE_FAILED = "failed"

# 失败原因的封闭取值(#565 G3 的对外契约,`backup` 那一档的子集)。同上:权威在
# `deploy/lambda/api/core/create_deadline.py` 的 `REASONS_FOR["backup"]`,这里是抄本,
# 由同一条断言比对。**只用得到这两个** —— 备份失败要么是备份本身没成(`backup_failed`),
# 要么是 host 侧没回执(`host_unreachable`);到点没跑完那一档由死线执行者写,不在本文件。
_REASON_BACKUP_FAILED = "backup_failed"
_REASON_HOST_UNREACHABLE = "host_unreachable"

# `_ssm_run` 在「压根没拿到裁决」时(预算用完仍无终态 / SSM API 本身失败)输出里带的哨兵。
# 归因靠它,不靠匹配散文 —— 措辞与异常类名都会变,哨兵不会。这个文件已有同款惯例
# (`OC_BACKUP_VM_LEFT_PAUSED` / `OC_BACKUP_SOURCE_ABSENT`)。
_SSM_NO_VERDICT = "OC_SSM_NO_VERDICT"

_BACKUP_TERM_GRACE_SEC = 60
"""#565 G1 —— 同步备份被 `timeout` TERM 掉之后,留给 `backup-data.sh` 的 EXIT trap 把 VM
从 Paused 恢复回 running 的宽限期。

**这个 60 有两个既有出处,本文件是第三处抄本**(同 `_PHASE_*` / `_REASON_*` 的处置理由:
本 Lambda 的 asset 只含 `handler.py`,import 不到那两处):
  · `deploy/userdata/host-agent.py:1104` 的 `_BACKUP_TERM_GRACE_SEC`(env 可覆盖)——
    本机定时备份那条路径已经在用它;
  · `deploy/lambda/api/core/create_deadline.py` 的 `BACKUP_TERM_GRACE_SEC` ——
    预算表按它给 backup 步留量。

推导:`backup-data.sh` 里 resume 的最坏耗时「5 次 × 5s max-time + (1+2+3+4) = 35s」+ 25s 余量。
`test_469_r7_host_backup_loop_adversarial.py::TestResumeBudgetFitsInTheTermGrace` 锁住脚本侧,
`tests/test_565_g1_budget_breakdown.py` 锁住这三处逐值一致 —— 谁改一处不改其余就红。
"""


def _mark_backup_phase(tenant_id, op_id, phase, reason=None, expect_phase=None,
                       require_unexpired=False, attempts=1):
    """把手动备份的相位写回租户行。返回 **True = 真的写进去了**。

    **三道锚(Codex 独立复审第 1 轮把只锚 op_id 的版本判成缺陷,核实为真):**

    1. `backup_op_id = :op` —— 世代锚。异步 Lambda 对未处理异常会**用同一 payload 重投**,
       客户也可能在上一次还没结束时又发起一次备份。没有它,一次陈旧的重投会覆盖一次**全新**
       备份的相位。
    2. `expect_phase` —— **来源相位**锚。光有世代锚不够:同一个 op_id 的**延迟重投**能把一个
       **已经被死线执行者判死的 `failed`** 翻回 `running`,然后在死线之后执行,最后再用
       `succeeded` 覆盖掉 `failed` —— 客户先被告知失败、后被告知成功,而它确实超了死线。
       所以起跑要求"当前仍是在飞相位",收尾要求"当前仍是 `running`"。
    3. `require_unexpired` —— **死线**锚(仅起跑用)。G3 给通道 B/C 都加了消费前的过期检查,
       通道 D(异步 invoke backup)之前没有 —— 这就是它。死线已过就不起跑,把执行也挡住,
       而不只是不写相位。

    **写失败不再被静默吞掉。** 原来那版一律 `print` 就算了,理由是"别让 DDB 抖动把一次已成功
    的备份变成异常触发重跑"。那个顾虑本身对,但漏了更糟的后果:备份**成功**而 `succeeded`
    这次写失败 → 行停在 `running` → 600s 后死线执行者把一次**成功的备份**报成失败,客户被
    告知了一个谎。所以改成:**有界重试**(瞬时抖动是常见情形,原地重试最便宜),并把最终
    结果返回给调用方去决定 —— 起跑写不进就**不备份**(不许跑一次没人跟踪的备份),收尾写不进
    就上抛(让它进 DLQ 告警;`backup_fn` 这次刚配上 DLQ)。
    """
    if not op_id:
        return True  # 不是手动备份(删前 / suspend / 定时批量)—— 它们没有句柄,也不该有相位
    expr = "SET backup_phase = :ph, backup_phase_at = :t"
    vals = {":ph": phase, ":t": _now_iso(), ":op": op_id}
    cond = ["backup_op_id = :op"]
    if expect_phase is not None:
        cond.append("backup_phase IN (" + ", ".join(
            f":e{i}" for i in range(len(expect_phase))
        ) + ")")
        for i, p in enumerate(expect_phase):
            vals[f":e{i}"] = p
    if require_unexpired:
        # 缺死线字段的行(升级期那批)照旧放行 —— 与 `create_deadline.is_expired()` 的
        # fail-safe 方向一致:不许因为缺字段就拒掉一次客户已受理的操作。
        cond.append("(attribute_not_exists(backup_deadline) OR backup_deadline > :now)")
        vals[":now"] = int(time.time())
    if reason is not None:
        expr += ", backup_fail_reason = :r, backup_fail_at = :t"
        vals[":r"] = reason
    last = None
    for i in range(max(1, attempts)):
        try:
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=expr,
                ConditionExpression=" AND ".join(cond),
                ExpressionAttributeValues=vals,
            )
            return True
        except Exception as e:  # noqa: BLE001 —— 条件不成立与瞬时故障在这里都返 False
            last = e
            if "ConditionalCheckFailed" in type(e).__name__ or (
                "ConditionalCheckFailed" in str(e)
            ):
                # 条件不成立 = **这次操作不是我的**(或已被判死)。重试不会让它变成立,
                # 直接放弃 —— 我无权改别人的状态。
                print(
                    f"[#564] backup phase {tenant_id} {op_id} -> {phase} 放弃:"
                    "条件不成立(已被判死 / 已是另一次操作)"
                )
                return False
            if i + 1 < max(1, attempts):
                time.sleep(1 + i)
    print(f"[#564] backup phase {tenant_id} {op_id} -> {phase} 写入失败: {last}")
    return False


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def lambda_handler(event, context):
    """Triggered by EventBridge schedule or API Gateway (manual backup)."""
    # Manual single-tenant backup via API
    tenant_id = event.get("tenant_id")
    if tenant_id:
        item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
        if not item:
            return {"error": "tenant not found", "success": False}
        # pre_delete=True 是 delete_tenant 的删前备份调用:此时租户 status 已被删除
        # CAS 翻成 "deleting"(tenant_service.py delete CAS 先于 backup),普通 running
        # 守卫会把它当 no-op 拒掉 → 删前备份形同虚设、盘照删(CRITICAL 数据丢失)。
        # 删前备份必须能备份 deleting/stopped 态:只要盘还在(host_id/vm_num 有)就能备。
        # 仍拒 already-deleted(盘已 rm,无可备)。非 pre_delete 的手动/定时备份保持
        # 只备 running 的原契约(停机态盘可能不一致,非删除场景不强备)。
        # #564 G7 —— 手动备份的句柄。只有它带 `backup_op_id`,所以它是"这是手动备份"的
        # 判别符;删前备份 / suspend 备份 / 定时批量都不带,相位写入对它们整体是 no-op
        # (客户表格明文只要"网关手动备份",定时备份的错峰语义不许被顺手改)。
        _op_id = event.get("backup_op_id")
        if event.get("pre_delete"):
            if item.get("status") == "deleted":
                return {"error": "tenant already deleted", "success": False}
        elif item.get("status") != "running":
            # 准入被拒也要落终态:客户手里已经有一个 202 和句柄,不写的话那个句柄永远查不到
            # 结果 —— 而 600s 后死线执行者会把它判成"到点没跑完",归因就错了(真实原因是
            # 租户不在 running)。
            _mark_backup_phase(
                tenant_id, _op_id, _PHASE_FAILED, _REASON_BACKUP_FAILED
            )
            return {"error": "tenant not running", "success": False}
        # **起跑的 CAS 决定要不要执行**,不只是"写个相位"。三道锚(世代 / 来源相位在飞 /
        # 死线未过)任一不成立就**不备份**:
        #   · 世代不对 → 这次投递属于一次已经被取代的操作;
        #   · 相位已是终态 → 它已经被死线执行者判死了,再跑一遍会在死线之后执行,而且最后
        #     还会用 succeeded 覆盖掉那个 failed(客户先被告知失败、后被告知成功);
        #   · 死线已过 → 通道 D 的"消费前过期检查"(G3 给通道 B/C 都做了,这里补齐)。
        # 写不进去也不备份:一次**没人跟踪**的备份比不备份更糟 —— 它会占 host、写 S3,而
        # 客户手里那个句柄永远查不到结果。
        if _op_id and not _mark_backup_phase(
            tenant_id, _op_id, _PHASE_RUNNING,
            expect_phase=(_PHASE_QUEUED, _PHASE_RUNNING),
            require_unexpired=True,
            attempts=3,
        ):
            return {
                "error": "backup not started: the operation was superseded, already "
                "finalized, or past its deadline",
                "success": False,
                "skipped": True,
            }
        # #565 G1 —— 单租户路径(删前备份 / suspend 同步备份 / 网关手动备份)的预算随事件来。
        # 缺失时 `backup_tenant` 回落默认 300s(升级期的在飞事件、以及不带预算的旧调用方)。
        _result = backup_tenant(item, ssm_budget_sec=event.get("ssm_budget_sec"))
        # `backup_tenant` 返 `{"tenant_id","success",...}`;success 才算成。
        if isinstance(_result, dict) and _result.get("success"):
            # 收尾锚在 `running` 上:期间若已被死线执行者判死,这次写不进去 —— 那是**对的**,
            # 它确实超了死线。写不进去且不是"条件不成立"(即真的写库故障)时**上抛**:
            # 备份成功而相位停在 `running`,600s 后执行者会把一次成功的备份报成失败 ——
            # 客户被告知一个谎。让它进 DLQ 告警(`backup_fn` 这次刚配上),代价是最坏多跑
            # 两次备份(AWS 异步重试 2 次),而起跑那道死线锚会把重跑限制在死线之内。
            if _op_id and not _mark_backup_phase(
                tenant_id, _op_id, _PHASE_SUCCEEDED,
                expect_phase=(_PHASE_RUNNING,), attempts=3,
            ):
                _cur = (
                    tenants_table.get_item(Key={"id": tenant_id}).get("Item") or {}
                ).get("backup_phase")
                if _cur == _PHASE_RUNNING:
                    # 相位还是 running → 不是"被判死",是真的没写进去。fail-loud。
                    raise RuntimeError(
                        f"backup for {tenant_id} succeeded but phase stayed "
                        f"{_cur!r} (op={_op_id}); the deadline executor would report "
                        "this successful backup as failed"
                    )
        else:
            # 归因二选一,判据是**哨兵**不是散文(见 `_SSM_NO_VERDICT` 的说明):
            #   · 压根没拿到裁决(预算耗尽无终态 / SSM API 失败)→ `host_unreachable`
            #   · 命令跑了但失败(打包/上传/校验)→ `backup_failed`
            # 两个值都在 `backup` 那一档的封闭子集里(#565 G3 已发布)。
            _err = str((_result or {}).get("error") or "")
            _reason = (
                _REASON_HOST_UNREACHABLE
                if _SSM_NO_VERDICT in _err
                else _REASON_BACKUP_FAILED
            )
            _mark_backup_phase(tenant_id, _op_id, _PHASE_FAILED, _reason)
        return _result

    # Scheduled run. PRD 2.6 要求"每用户错峰备份(非开源版写死统一时间)+ 队列限并发,
    # 避免大量用户/机器同刻备份"。实现:EventBridge 高频触发(如每 30min),每次只挑
    #   ① 距上次备份已超过 BACKUP_INTERVAL_HOURS 的租户(到期才备,天然错峰)
    #   ② 本批最多 BACKUP_BATCH_LIMIT 个(限并发,削峰)
    # 这样每用户按自己上次备份时间错峰滚动,而不是全量同刻触发。
    interval_h = int(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
    batch_limit = int(os.environ.get("BACKUP_BATCH_LIMIT", "20"))
    now_dt = _now_dt()
    due = []
    last_evaluated = None
    while True:
        scan_kw = {
            "FilterExpression": "#s = :r",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":r": "running"},
        }
        if last_evaluated:
            scan_kw["ExclusiveStartKey"] = last_evaluated
        page = tenants_table.scan(**scan_kw)
        for t in page.get("Items", []):
            if _backup_due(t, now_dt, interval_h):
                due.append(t)
        last_evaluated = page.get("LastEvaluatedKey")
        if not last_evaluated:
            break
    # 错峰:最久没备份的优先(last_backup_at 升序;从未备份的排最前)
    due.sort(key=lambda t: t.get("last_backup_at") or "")
    batch = due[:batch_limit]
    results = [backup_tenant(t) for t in batch]
    return {
        "due_total": len(due),
        "batched": len(batch),
        "deferred": max(0, len(due) - len(batch)),
        "interval_hours": interval_h,
        "results": results,
    }


def _backup_due(tenant, now_dt, interval_h):
    """到期判断:从未备份 → 立即到期;否则距 last_backup_at 超过 interval_h 才到期。"""
    from datetime import datetime, timezone

    last = tenant.get("last_backup_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True  # 解析不了当作到期,宁可多备不漏备
    return (now_dt - last_dt).total_seconds() >= interval_h * 3600


def _now_dt():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


_DEFAULT_SSM_BUDGET_SEC = 300
"""备份的 SSM 墙钟预算**默认值** —— 只给系统定时备份用。

#565 G1 —— **同一个 backup Lambda 被四种方式调用,它们处在不同的死线档下**,所以一个数管不了
四种:

| 调用方式 | 死线 | 该给的预算 | 为什么不同 |
| --- | --- | --- | --- |
| suspend 的同步删前备份 | 180s | **90s** | 挤在 180s 里,还要给 stop-vm 留 30s |
| delete 的同步删前备份 | 600s | **90s** | 与 suspend 同源(同一段内联逻辑),口径一致 |
| 网关手动备份(异步) | 600s | **300s** | 异步、不受调用侧 `read_timeout` 约束,大盘友好 |
| 系统定时备份(EventBridge) | 无 | 300s(本默认值) | 客户表格明文不在本轮范围,不动 |

**预算走事件传入,不 import 口径模块** —— 本 Lambda 的 asset 只含自己的 `handler.py`,
`core/create_deadline.py` 在这里 import 不到(#564 已确认的跨 Lambda 边界)。复制一份常量
就等于第二个真相源,所以改成由调用方把数带过来;两侧不漂移由机械断言守(见
`tests/test_565_g1_budget_breakdown.py`)。
"""


def backup_tenant(tenant, ssm_budget_sec=None):
    tid = tenant["id"]
    host_id = tenant["host_id"]
    now = _now()

    # #565 G1 —— 预算由调用方给定(见 `_DEFAULT_SSM_BUDGET_SEC` 的四种调用方式表)。
    # **不可解析或非正一律回落默认值**:一个坏预算若被当 0 用,备份会立刻放弃并报失败,
    # 而 fail-closed 会据此回滚一次本可成功的 suspend/delete —— 那比用一个偏大的默认值更糟。
    try:
        _budget = int(ssm_budget_sec)
        if _budget <= 0:
            _budget = _DEFAULT_SSM_BUDGET_SEC
    except (TypeError, ValueError):
        _budget = _DEFAULT_SSM_BUDGET_SEC
    # 这一步的墙钟上界 = `_budget`,拆成「脚本额度 + TERM 宽限」两段(理由见下面 `timeout` 处)。
    # **宽限必须真的留出来**:装不下就说明调用方给的预算比宽限还小,那时给脚本留 1s 也没有
    # 意义 —— 退回默认预算并 fail-loud 到日志,由 G8 校验器/压测去暴露那个坏配置,
    # 而不是在这里静默造一个必然 SIGKILL 的窗口。
    _grace = _BACKUP_TERM_GRACE_SEC
    if _budget <= _grace:
        print(
            f"[#565] backup 预算 {_budget}s <= TERM 宽限 {_grace}s,留不出让 VM 从 Paused "
            f"恢复的时间;回落默认 {_DEFAULT_SSM_BUDGET_SEC}s"
        )
        _budget = _DEFAULT_SSM_BUDGET_SEC
    _script_budget = _budget - _grace

    # Existing hosts do not rerun init-host.sh after a control-plane deploy.
    # #545 —— freshness 判据必须是【本次新增】的标记,不能用旧哨兵。存量 host 的
    # 自愈分支永远跳过 → 带 guest flush 的新版永不被拉取 → 线上修复静默不生效(memory
    # host-script-s3-asset-drift 反复踩)。改判 oc_flush_guest:缺它 = 旧版无 flush =
    # 备份丢未落盘客户数据,必须先从 S3 权威前缀装新版再跑。bash -n + 双 grep 守住
    # "只装语法合法且确含新语义的脚本",装不上就 exit 1 fail-closed(不拿旧版静默备份)。
    #
    # 上面那段说明为什么不能用旧哨兵当判据,而它自己现在也成了旧哨兵:存量 host 的
    # backup-data.sh 早就有 oc_flush_guest(#545),却可能缺 R7 这一批语义 ——
    # per-tenant flock(与 launch/stop/delete/migrate/备份互斥)、S3 key 带 run id
    # (同秒两次备份不撞 key)、以及本轮改的"`.key` 先传、`.enc` 最后传"。
    # 只判 oc_flush_guest 会让自愈分支对这些 host 永远跳过,而**滚动升级期间中心调度
    # 仍是开着的**(config 要求先铺完 host-agent 再置 false),于是那段时间里中心侧会
    # 拿旧脚本持续产出撕裂或解不开的恢复点 —— 正是这条判据要防的那件事本身。
    #
    # 三个哨兵与 host-agent 的 _BACKUP_SCRIPT_SENTINELS 【同源】(deploy/userdata/
    # host-agent.py),两处各写一套必然漂移。有测试钉住同源:一处改,两处都红。
    _sentinels = ("oc_flush_guest", "OC_BACKUP_SOURCE_ABSENT", "_RUN_ID")
    _installed_gate = " || ".join(
        f"! grep -q {s} /home/ubuntu/backup-data.sh 2>/dev/null" for s in _sentinels
    )
    _download_gate = "".join(
        f"grep -q {s} /tmp/oc-heal-backup-data.sh && " for s in _sentinels
    )
    cmd = (
        f"if {_installed_gate}; then "
        "[ -r /etc/platform.env ] && { set -a; . /etc/platform.env; set +a; }; "
        'aws s3 cp "s3://${ASSETS_BUCKET:?}/deployment/scripts/backup-data.sh" '
        "/tmp/oc-heal-backup-data.sh --no-progress >/dev/null 2>&1 && "
        "bash -n /tmp/oc-heal-backup-data.sh && "
        f"{_download_gate}"
        "install -o root -g root -m 755 /tmp/oc-heal-backup-data.sh "
        "/home/ubuntu/backup-data.sh || exit 1; "
        "fi && "
        # #565 G1 —— **host 侧的墙钟界限,TERM 优先。**
        #
        # Codex 独立复审正确地指出:此前只有控制面的轮询被界住,`backup-data.sh` 在 host 上
        # 仍跑在 SSM 的 `executionTimeout` 缺省 **3600s** 下。于是把预算从 300 降到 90 反而
        # 制造了一个**新的假失败带**:一次耗时 90–300s 的备份,改前控制面会等到它成功,
        # 改后 90s 就报失败 → fail-closed 回滚 suspend/delete → 备份随后成功 →
        # **上层失败、底层成功**,正是本 issue 要消灭的形态。
        #
        # **为什么不用 SSM 的 `executionTimeout`**:本脚本的形态是 Pause VM → 压缩 → Resume,
        # Pause 窗口包住整个压缩;而 SSM 终止命令**没有文档化的宽限期**。脚本自己
        # (`deploy/userdata/backup-data.sh:69-70/83-84/133`)已经把后果写死:
        # 「EXIT trap 里的 Resume 会被 SIGKILL 掐断,客户 VM 永久留在 Paused」
        # 「这比丢一次备份严重:丢备份下轮会重来,而 Paused 不会自己好」,且「reaper 救不了它」
        # (判 `fc_alive` 是进程存活,一个 Paused 的 Firecracker 进程是活的)。
        #
        # **所以对齐 host-agent 那条已被评审过的机制**:先 SIGTERM,给 EXIT trap
        # `BACKUP_TERM_GRACE_SEC` 秒把 VM 恢复回 running,仍不退出才 SIGKILL。
        # `timeout --signal=TERM --kill-after=<grace> <T>`,其中
        # **T = 本步预算 - grace**,于是这一步的墙钟上界恰好是本步预算 —— 预算第一次真的
        # 界住了 host,而不只是界住控制面的轮询。
        #
        # 已知残余(如实记下,不假装消灭):bash 把信号**延迟到当前前台命令返回之后**才跑
        # trap,所以一次卡住的 `aws s3 cp` 仍可能耗尽宽限而被 SIGKILL。这与 host-agent 那条
        # 路径的风险画像**逐字相同**(同一个脚本、同一套 SIGTERM+60s),所以不是新增风险面;
        # 要彻底消灭得让脚本自己限时,归后续。
        f"timeout --signal=TERM --kill-after={_grace} {_script_budget} "
        f"/home/ubuntu/backup-data.sh {tid} {BUCKET} {PREFIX} {CMK_KEY_ID}"
    )
    success, output = _ssm_run(host_id, cmd, timeout=_budget)

    result = {"tenant_id": tid, "success": success, "timestamp": now}
    #
    # 为什么必须回传:备份失败时 suspend 会把 status 回滚成 running/stopped。但如果失败的原因
    # 而 reaper 救不了它:reaper 的 fc_alive 是进程存活检查,一个 Paused 的 Firecracker 进程
    # 照样活着,它会得出同样的错误结论。所以这个事实只有 backup-data.sh 知道,必须由它上报。
    #
    # 判据是稳定哨兵而不是措辞:backup-data.sh 的 EXIT trap 在【最终仍未恢复】时打
    # OC_BACKUP_VM_LEFT_PAUSED(主路径失败但 trap 补救成功时【不】打 —— 那时 VM 已经回来了)。
    # 通道复用现成的 stdout(下面抽 S3 key 用的是同一份 output),不新增接口。
    if "OC_BACKUP_VM_LEFT_PAUSED" in (output or ""):
        result["vm_left_paused"] = True
        print(
            f"Backup left {tid} PAUSED (sentinel OC_BACKUP_VM_LEFT_PAUSED in host output);"
            " the caller must NOT roll the tenant back to an active status"
        )
    if success:
        # 脚本把 key echo 到 stdout 最后一行(`${PREFIX}/${tid}/<ts>.gz[.enc]`,见
        # backup-data.sh:92/100);上游 suspend 用它精确定位【本次】产物做 restore。
        # 不能靠 Lambda 的 now(isoformat 微秒)去猜脚本用 `date +...Z`(秒精度)命名的对象
        # ——两者独立生成、格式不同,精确匹配必落空。key 只能来自脚本真实输出。
        # 提取不到 key(输出异常/被日志污染)→ 视作失败 fail-closed:宁可让 suspend 502
        # 重试,也不回传空 key 让上游删盘后无从恢复(no-data-loss)。
        backup_key = ""
        for line in reversed((output or "").splitlines()):
            cand = line.strip()
            if cand.startswith(f"{PREFIX}/{tid}/") and (
                cand.endswith(".gz") or cand.endswith(".gz.enc")
            ):
                backup_key = cand
                break
        if not backup_key:
            result["success"] = False
            result["error"] = (
                "backup-data.sh returned Success but no S3 key found in output; "
                "treating as failure to avoid data loss on suspend/delete. "
                f"tail={(output or '')[-200:]!r}"
            )
            print(f"Backup key missing for {tid}: {result['error']}")
            return result
        result["backup_key"] = backup_key
        # ㉜ last_backup_at 必须条件写:租户仍在【我们派发的那台 host】上
        #
        # 本函数在开头读租户拿到 host_id,然后同步跑一条 300s 超时的 SSM。迁移就发生在
        # 这几十秒到几分钟里。若租户已经搬到别的 host,这次备的是【旧 host 上的旧盘】,
        # 而无条件写 last_backup_at 会让新 owner 机以为"刚备过"而跳过它 —— 于是那个租户
        # 在新 host 上迟迟不被备份,而系统显示一切正常。
        #
        # 判据取的是【备份开始时】读到的 host_id:条件不成立说明期间搬过家,这次结果不作数。
        # 与 R7 侧的写完全同源(host-agent.py 那处的三重条件里也有 host_id = :self)——
        # 那边已经这么做了,而这边漏了。**同一件事的两条路,只加固一条等于没加固。**
        #
        # attribute_exists(id) 同理:租户可能在这几分钟里被删掉,无条件写会 upsert 出一个
        # 只有 id + last_backup_at 的僵尸行。
        # 用函数开头已经取好的 host_id(:101)——【备份开始时】读到的那个,正是条件要锚的值。
        # ⚠ 我第一版写的是 `item.get("host_id")`,而本函数的参数叫 `tenant`,没有 item。
        # 那会抛 NameError,而它恰好落在下面这个 `except Exception` 里 → 被当成"条件不成立"
        # → **条件写永远不执行、永远打"没推进"**,整个修复静默失效,而外部看不出区别。
        # 是新加的那两条测试立刻抓到的 —— 这就是"新行为必须有测试"的用处。
        _bk_host = host_id
        try:
            _kw = {
                "Key": {"id": tid},
                "UpdateExpression": "SET last_backup_at = :t",
                "ExpressionAttributeValues": {":t": now},
                "ConditionExpression": "attribute_exists(id)",
            }
            if _bk_host:
                _kw["ConditionExpression"] = (
                    "attribute_exists(id) AND host_id = :bkhost"
                )
                _kw["ExpressionAttributeValues"][":bkhost"] = _bk_host
            tenants_table.update_item(**_kw)
        except Exception as _ce:  # noqa: BLE001 — CCF 不是错误,是"期间搬家/被删了"
            # 备份本身是成功的(S3 对象已落地),所以不把 success 翻成 False —— 上游
            # (suspend/delete 的删前备份)靠 backup_key 决定能不能删盘,谎报失败会让它
            # 白白重试甚至拒绝删除。这里只是"不推进时间戳",并 fail-loud 说清原因。
            result["last_backup_at_not_advanced"] = True
            print(
                f"Backup {tid}: object uploaded ({backup_key}) but last_backup_at was NOT "
                f"advanced — the tenant no longer matches host_id={_bk_host!r} or was "
                f"deleted during the backup ({type(_ce).__name__}). This backup captured "
                "the OLD host's disk; the new owner must back it up again."
            )
    else:
        result["error"] = output
        print(f"Backup failed for {tid}: {output}")

    return result


def _ssm_run(instance_id, command, timeout=300):
    """等一条 SSM 命令跑完;`timeout` 是**墙钟预算**,不是轮数。

    #565 G1-a 第二条要求「backup 侧 300s SSM 上界与死线预算**对齐**」—— 对齐的前提是
    这个数**真的是**上界。原来的循环是 `for _ in range(timeout // 3)`,即
    **轮数上限**;它等于上界只在「每轮恰好 3s」这个隐含假设下成立。

    **#573 把那个假设打破了**:它给上面的 `ssm` client 加了
    `retries={"max_attempts": 8, "mode": "adaptive"}`(为了防 SendCommand 节流毒 DLQ,
    那件事本身是对的)。于是单次 `get_command_invocation` 在被节流时最坏要等 7 次重试的
    指数退避 —— botocore `ExponentialBackoff` 的 `_MAX_BACKOFF=20`、基数 2,实测退避总和
    上界 `1+2+4+8+16+20+20 = 71s`,adaptive 的客户端 token-bucket 限速还在这之上。
    一轮就可能 74s,100 轮的理论上界约 7400s —— 实际是被本 Lambda 自己的 900s 外壳杀掉。

    后果链(正是 #565 G1-a 修掉的那个病回归):调用侧 `_force_backup_sync` 的
    `read_timeout` 按「305s 上界」设的,于是**调用侧先放弃、而本函数还在跑并会写 S3** →
    上层看到失败、底层其实备成功了。

    所以改成真实 deadline,且**检查放在发下一次 API 调用之前** —— 超预算就不再发新请求。
    净上界 = `timeout` + 最后那次调用自己的耗时(最坏 ~71s 退避),这个数是可算的,
    调用侧的 `read_timeout` 按它取(见 `core/clients.BACKUP_SYNC_INVOKE_CONFIG`)。

    `time.monotonic()` 而不是 `time.time()`:后者会被 NTP 校正拖动,预算判定不能受它影响。
    """
    try:
        # #565 G1(Codex 独立复审第 2 轮)—— **计时必须从 `send_command` 之前开始。**
        # 原来起在它之后,于是 `timeout` 只界住轮询、**不界住 send 本身** —— 而 #573 给 ssm
        # client 加了 adaptive 重试之后,一次被节流的 `SendCommand` 最坏也要 ~71s 退避。
        # 那 71s 白白落在预算之外,于是「执行段」不是真实上界,调用方按它排的死线就会破。
        # `time.monotonic()` 而非 `time.time()`:后者会被 NTP 校正拖动。
        _deadline = time.monotonic() + timeout
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            # **⚠ 这里刻意【不】设 `executionTimeout`,理由见下 —— 这不是遗漏。**
            #
            # Codex 独立复审第 1 轮正确地指出:`TimeoutSeconds` 只是**投递**超时,命令一旦开始
            # 跑就与它无关;`executionTimeout` 才是执行超时,而 `AWS-RunShellScript` 的缺省是
            # **3600s**。所以 #565 G1 把这里的预算从 300 降到 90/55 只约束了**控制面的轮询**,
            # host 上的 `backup-data.sh` 仍可跑到一小时 —— 那确实是一个真缺陷。
            #
            # **但直接加 `executionTimeout` 会引入一个更糟的后果,所以本 MR 不加。**
            # `backup-data.sh` 的形态是 **Pause VM → 压缩 → Resume**(该脚本 :146-148),
            # Pause 窗口包住整个压缩。而 SSM 在 executionTimeout 到点时如何终止命令
            # **没有文档化的宽限期**;脚本自己 :83-84 已经把这个后果写死了:
            #   「EXIT trap 里的 Resume **会被 SIGKILL 掐断**,客户 VM 永久留在 Paused」
            #   「这比丢一次备份严重:丢备份下轮会重来,而 Paused 不会自己好」
            # 而且 :133 记着 **reaper 救不了它** —— reaper 判 `fc_alive` 是进程存活检查,
            # 一个 Paused 的 Firecracker 进程是活的。也就是说加了它会造出一个**没有任何
            # 自动收敛机制**的永久 Paused VM。
            #
            # **正确的机制仓库里已经有了,但只装在另一条调用路径上。** host-agent 驱动的
            # 本机定时备份是「先 SIGTERM、等 `_BACKUP_TERM_GRACE_SEC`=60s、再 SIGKILL」,
            # 那 60s 是按 EXIT trap 里 resume 的最坏耗时 35s 算出来的(该脚本 :77-91,
            # 并有 `test_469_r7...::TestResumeBudgetFitsInTheTermGrace` 把两个数锁在一起)。
            # SSM 这条路径要对齐,得把命令包成
            # `timeout --signal=TERM --kill-after=60 <budget> bash backup-data.sh …`,
            # 而那会让这一步对执行段的占用变成 `budget + 60` —— 60s 的宽限在 180s 档里装不下
            # (suspend 的执行段只有 120s,还要给 stop-vm 30s)。**那是一个死线口径问题,
            # 不是一行代码问题**,已连同 Codex 第 2 条一起记进 MR 与 changelog 交由 owner 判决。
            #
            # host 侧的界限**已经用下面那个 `timeout --signal=TERM` 包装做到了**,
            # 所以这里不设 `executionTimeout` 不再留下"host 无上界"的缺口。
            Parameters={"commands": [command]},
            TimeoutSeconds=int(timeout) + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(5)
        while time.monotonic() < _deadline:
            result = ssm.get_command_invocation(
                CommandId=cmd_id,
                InstanceId=instance_id,
            )
            if result["Status"] == "Success":
                return True, result.get("StandardOutputContent", "")
            if result["Status"] in ("Failed", "TimedOut", "Cancelled"):
                output = result.get("StandardOutputContent", "")
                error = result.get("StandardErrorContent", "")
                return False, "\n".join(part.rstrip() for part in (output, error) if part)
            time.sleep(3)
        # #564 G7 —— 这两支是「**压根没拿到裁决**」:预算用完仍无终态,或连 SendCommand /
        # GetCommandInvocation 都没成功。与「命令跑了但失败」(上面那个 Failed/TimedOut/
        # Cancelled 分支)在归因上是**不同的值**:前者是 `host_unreachable`,后者是
        # `backup_failed`。
        #
        # 用**稳定哨兵**而不是让调用方去匹配散文("timeout"/"no ssm"/异常类名):
        # 措辞会变、异常类名更是随 botocore 版本变,而这个文件本身已经建立了哨兵惯例
        # (`OC_BACKUP_VM_LEFT_PAUSED` / `OC_BACKUP_SOURCE_ABSENT`)。照它办。
        return False, f"{_SSM_NO_VERDICT}: budget {timeout}s exhausted with no terminal status"
    except Exception as e:
        return False, f"{_SSM_NO_VERDICT}: {type(e).__name__}: {e}"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
