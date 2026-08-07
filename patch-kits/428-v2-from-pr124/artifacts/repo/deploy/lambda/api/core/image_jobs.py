# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#394 step1 — 持久化 pull Job 记录(ADR §4.4)。

为什么存在:今天 pull 的进度真值只在 host 的 `/tmp/<job_id>.txt`(host_service.py:831),
host 一重启就没了,且 `pull_image_progress` 只能读 host 当前 `pull_command_id`(单值)——
查不了历史任务,也分不清并发的不同 pull 请求。本模块把 Job 落到 DDB,让:
  · progress 能按 job_id 精确查(而不是"该 host 最近那次");
  · worker 早退 / host 重启后终态仍在(不再永卡 InProgress);
  · 同一 Idempotency-Key 重放返回同一 job(ADR §4.9)。

本步【不改】现有 live pull 行为:Job 是旁路记录,写失败不阻断 pull(fail-open,见
`record_transition` 注释)。真正把 Job 当唯一真相源是 step 4-5 的事。

状态机(ADR §4.4):
    QUEUED → STAGING → VALIDATING → COMMITTING → SUCCEEDED
                                            └→ FAILED
                                            └→ RECOVERY_REQUIRED
"""

import os
import time

import core.clients as clients
from core.utils import _now


def _table():
    """惰性取表句柄(表 env 未配 → None,所有函数降级成 no-op)。

    刻意【不】在模块级快照 `clients.image_jobs_table`:core.clients 可能先于本模块被
    别的入口 import 并缓存,模块级绑定会把"当时那一刻"的 None 永久固化 —— 部署了表的
    环境也会静默降级成 no-op(而且完全不报错,是最难查的一类)。每次调用读一次,便宜且正确。
    """
    return getattr(clients, "image_jobs_table", None)


# 终态:progress 读到这些值就不必再探 host 进度文件(ADR §7"Host 重启 /tmp 丢失"一行)。
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "RECOVERY_REQUIRED")

# 非终态,按 ADR §4.4 的推进顺序。
ACTIVE_STATES = ("QUEUED", "STAGING", "VALIDATING", "COMMITTING")

VALID_STATES = ACTIVE_STATES + TERMINAL_STATES

# Job 记录保留 30 天后由 DDB TTL 回收(排障够用,不无限长);TTL 属性名与建表一致。
_TTL_DAYS = 30


def _ttl_epoch():
    return int(time.time()) + _TTL_DAYS * 86400


def is_enabled():
    """本步是否已部署(Job 表已配置)。

    调用方据此区分"查不到这个 job"与"本环境根本没有 Job 表":前者应 404 JOB_NOT_FOUND,
    后者必须保持旧行为(照传入 id tail 进度文件),不能把未部署谎报成 job 不存在。
    """
    return _table() is not None


def _table_name(table, env_name):
    """Return a concrete DynamoDB table name for low-level transactions."""
    name = getattr(table, "table_name", None)
    if isinstance(name, str) and name:
        return name
    return os.environ.get(env_name, "")


def create(job_id, instance_id, target_slot, snapshot_time, idempotency_key=None,
           snapshot_table=None):
    """落一条 QUEUED Job。返回 True=已写,False=未配置/条件失败/写失败。

    当 ``snapshot_table`` 传入时，Job Put 与 snapshot ACTIVE ConditionCheck 在同一个
    TransactWriteItems 中完成。这是全局删除的线性化点：
      · Job 事务先赢 → delete 随后进入 DELETING 后会在 active-job scan 看见它并回滚；
      · delete 先把 snapshot 置 DELETING → Job 事务条件失败，绝不会受理已下架版本。
    因此关闭“delete 扫描为空后、新 Pull 插入、delete 再标 deleted”的 TOCTOU 窗口。

    不传 ``snapshot_table`` 保留普通 conditional Put，供非 snapshot 调用和单元测试使用。
    Idempotency-Key 的“同 key 返回同 job”由入口查询实现。
    """
    table = _table()
    if table is None:
        return False
    item = {
        "job_id": job_id,
        "instance_id": instance_id,
        "target_slot": target_slot,
        "requested_snapshot_time": snapshot_time,
        "state": "QUEUED",
        "phase": "queued",
        "progress_percent": 0,
        "created_at": _now(),
        "updated_at": _now(),
        "attempt_id": 1,
        "fence_epoch": 0,
        "expires_at": _ttl_epoch(),
    }
    if idempotency_key:
        item["idempotency_key"] = idempotency_key

    if snapshot_table is not None:
        job_table_name = _table_name(table, "IMAGE_JOBS_TABLE")
        snapshot_table_name = _table_name(snapshot_table, "VERSION_SNAPSHOTS_TABLE")
        if not job_table_name or not snapshot_table_name:
            return False
        # #394 —— 传【原生 Python 值】,不预先 TypeSerializer。resource 的 .meta.client 已挂
        # 文档序列化 transform,会把 dict/str/int 序列化一次;若这里再手动序列化,transform 会
        # 二次包裹({'S':'x'}→{'M':{'S':...}})→ DynamoDB Type mismatch,每次 pull 都 503
        # (#394 真机 SSM 执行暴露,mock 不校验序列化边界)。
        try:
            table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": snapshot_table_name,
                            "Key": {"snapshot_time": snapshot_time},
                            "ConditionExpression": (
                                "attribute_exists(snapshot_time) AND "
                                "(attribute_not_exists(#st) OR #st = :active)"
                            ),
                            "ExpressionAttributeNames": {"#st": "status"},
                            "ExpressionAttributeValues": {":active": "active"},
                        }
                    },
                    {
                        "Put": {
                            "TableName": job_table_name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(job_id)",
                        }
                    },
                ]
            )
        except Exception as e:  # noqa: BLE001
            print(f"[pull] WARN guarded image_jobs.create failed job={job_id}: {e}")
            return False
        return True

    ccf = table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(job_id)"
        )
    except ccf:
        return False
    except Exception as e:  # noqa: BLE001
        # 调用方在准入闸之后检查 False 并负责释放 lease/status，不能向外抛而泄漏准入。
        print(f"[pull] WARN image_jobs.create failed job={job_id}: {e}")
        return False
    return True


def get(job_id):
    """按 job_id 读一条 Job;不存在或表未配置 → None。"""
    table = _table()
    if table is None or not job_id:
        return None
    return table.get_item(Key={"job_id": job_id}).get("Item")


def record_transition(job_id, state, phase=None, progress_percent=None,
                      result=None, error=None):
    """推进 Job 状态。

    fail-open(本步刻意):Job 是旁路记录,DDB 写失败【不】让正在跑的 pull 失败——否则
    step1 就把一条新的失败路径塞进既有 live pull(违反"不改变现有 live 路径")。真正
    fail-closed 的语义留到 step 4-5 把 Job 提成真相源时再收紧。调用方不看返回值也安全。
    """
    table = _table()
    if table is None or not job_id:
        return False
    if state not in VALID_STATES:
        raise ValueError(f"invalid pull job state: {state!r}")
    expr = ["#s = :s", "updated_at = :t"]
    names = {"#s": "state"}
    values = {":s": state, ":t": _now()}
    if phase is not None:
        expr.append("#p = :p")
        names["#p"] = "phase"
        values[":p"] = phase
    if progress_percent is not None:
        expr.append("progress_percent = :pct")
        values[":pct"] = int(progress_percent)
    if result is not None:
        expr.append("#r = :res")
        names["#r"] = "result"
        values[":res"] = result
    if error is not None:
        expr.append("#e = :err")
        names["#e"] = "error"
        values[":err"] = error
    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:  # noqa: BLE001 — 见 docstring:旁路记录不阻断 pull
        return False
    return True


def find_by_idempotency_key(instance_id, idempotency_key):
    """同 Idempotency-Key 重放 → 返回原 Job(ADR §4.9)。

    用 GSI 按 (instance_id, idempotency_key) 查。表未配置 / 无 key → None(调用方照常新建)。
    """
    table = _table()
    if table is None or not idempotency_key or not instance_id:
        return None
    resp = table.query(
        IndexName=os.environ.get("IMAGE_JOBS_IDEMPOTENCY_INDEX", "gsi_idempotency"),
        KeyConditionExpression=(
            "instance_id = :i AND idempotency_key = :k"
        ),
        ExpressionAttributeValues={":i": instance_id, ":k": idempotency_key},
        Limit=1,
    )
    items = resp.get("Items") or []
    return items[0] if items else None


def latest_for_host(instance_id):
    """该 host 最近一条 Job(兼容窗口:progress 不传 job_id 时用,ADR §4.4)。

    用 GSI 按 instance_id 分区 + created_at 倒序取 1 条。
    """
    table = _table()
    if table is None or not instance_id:
        return None
    resp = table.query(
        IndexName=os.environ.get("IMAGE_JOBS_HOST_INDEX", "gsi_host_created"),
        KeyConditionExpression="instance_id = :i",
        ExpressionAttributeValues={":i": instance_id},
        ScanIndexForward=False,  # created_at 倒序 → 最新在前
        Limit=1,
    )
    items = resp.get("Items") or []
    return items[0] if items else None


def active_jobs_for_snapshot(snapshot_time, limit=10, success_grace_s=0):
    """#394 P1-3 —— 返回【正在拉取该 snapshot】的 Job(供全局删除的 in-flight 保护)。

    删除保护的盲点:一个已受理的 pull 可能【还没把版本提交进 slots.json】,此时 slots 引用扫描
    看不到它;若此刻删掉快照记录,那个 worker 随后会把一个"已下架、以后拉不回"的版本装进
    live/canary(no-data-loss)。故删除前必须把在飞 Job 纳入引用判据:任何 ACTIVE_STATES 的
    Job 的 requested_snapshot_time == 目标 → 拒删。

    #394(codex B2)—— 刚 SUCCEEDED 的 Job 也要短暂保护:commit 时的 mirror 回写是 best-effort
    (|| true),失败时 mirror 仍是旧 canary 指针,但心跳几秒前刚把 synced_at 刷新过 → 新鲜度门
    误判"目标不在任何 slot"。心跳每 ~15s 覆盖一次 mirror,故 SUCCEEDED 后 success_grace_s 秒内
    (调用方传 mirror 最大陈旧窗口)仍视作占用,等下一轮心跳把真值同步进 mirror。收敛:超过该
    窗口必有一次 post-commit 心跳刷新过 mirror,slots 扫描即可看到,不再需要 Job 兜底。

    无 snapshot_time GSI(删除低频),整表 scan + filter 可接受;翻页到底(漏页 = 漏保护)。
    表未配置 → 空(该环境没有持久化 Job,退回 slots/租户引用判据)。"""
    table = _table()
    if table is None or not snapshot_time:
        return []
    states = "#st IN (:queued, :staging, :validating, :committing)"
    values = {
        ":s": snapshot_time,
        ":queued": "QUEUED", ":staging": "STAGING",
        ":validating": "VALIDATING", ":committing": "COMMITTING",
    }
    if success_grace_s > 0:
        # updated_at 是 SUCCEEDED 落库时刻(record_transition 写);grace 窗口内的 SUCCEEDED 也拦。
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - success_grace_s)
        )
        states = f"({states} OR (#st = :succeeded AND updated_at > :cutoff))"
        values.update({":succeeded": "SUCCEEDED", ":cutoff": cutoff})
    # codex NB1 —— 强一致读:这是全局删除的 in-flight 保护判据,漏掉刚 admit 的 Job = 删掉
    # 正在拉取的版本(no-data-loss)。最终一致 Scan 可能看不到几毫秒前 TransactWriteItems 写入
    # 的 Job。主表 Scan 支持 ConsistentRead=True。
    kwargs = {
        "FilterExpression": f"requested_snapshot_time = :s AND {states}",
        "ExpressionAttributeNames": {"#st": "state"},
        "ExpressionAttributeValues": values,
        "ProjectionExpression": "job_id, instance_id, target_slot, #st, updated_at",
        "ConsistentRead": True,
    }
    found = []
    while True:
        resp = table.scan(**kwargs)
        found.extend(resp.get("Items") or [])
        lek = resp.get("LastEvaluatedKey")
        if not lek or len(found) >= limit:
            break
        kwargs["ExclusiveStartKey"] = lek
    return found[:limit]
