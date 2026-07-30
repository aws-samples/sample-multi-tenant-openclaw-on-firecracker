# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/name_dedup — #240 owner 作用域活跃 name 去重(治 api-key+只传 name 双开).

背景(客户真机 bug):api-key 直连,1 秒内两次相同 name 的 POST /tenants,因接口
~2s 才返回,客户端没收到第一次响应就发第二次 → 建两台。现有两套幂等防线对这个
场景双双熄火:client_token 幂等要客户传 client_token(没传 → id 走 name-<随机码>,
每次不同);inflight 锁键 owner#tenant_user_id 且 tenant_user_id 空就跳过(api-key
不传 → 不上锁)。见 [[inflight_dedup]] 与 core.utils._gen_id 的 #95 注释。

本模块补第三层:**活跃租户 name 去重**。与 inflight 锁的关键差异:
- inflight 锁只守"在途并发窗口",创建成功即 release(防不住"第一次成功、第二次晚到")。
- name 锁**跟随租户活跃生命周期**:创建成功 NOT release,只有 delete 达终态或创建
  失败回滚才 release。所以能防晚到重放(第一次已 running,第二次同 name → 409)。

**占位自身的年龄(created_at + TTL)是活跃性的主判据,不是它指向的 tenant 记录**——
这是并发正确性的关键(reviewer #2 抓出的双开根因):acquire_name_lock 在 tenant 记录
put_item **之前**跑(异步队列路径甚至隔几秒),若靠"查 tenant 是否存在"判活跃,并发
输家会因赢家 tenant 记录尚未写而误判僵尸→抢占→双开。改判据:
- 占位**新鲜**(created_at 在 _LOCK_TTL 内)→ 视为在途/活跃赢家占用 → 409,**绝不抢占**。
- 占位**过期**(created_at 超 _LOCK_TTL)→ 僵尸(release 漏了/进程崩了/租户早删)→ 抢占。
- 附加早释放信号:占位指向的 tenant 记录已确认 deleted(记录在且 status=deleted)→
  即便未过 TTL 也可抢占(软删后立即重建同名,不必等 TTL)。但"记录不存在"**不**触发
  抢占(可能是赢家还没写)——只有"记录在且明确 deleted"才早释放。

设计要点:
- 占位 item 放 tenants 主表(PK = "activename#<scope>#<name>"),复用已有表,不新建
  DDB 表、不碰 CDK RemovalPolicy(与 inflight_dedup 同源)。
- **刻意不设 DDB 真实 TTL**:name 锁是生命周期作用域(租户活多久锁多久,可能数天),
  而 openclaw-tenants 表真实 TTL 属性是 `inflight_ttl`(storage.py:50,DDB 每表仅一个)。
  若把占位 TTL 指向它,DDB sweeper(盲判、不看租户状态)会在 ~30min 后把**还在
  running 的活租户** name 锁物理删除 → 下次同名 create 的 attribute_not_exists(id)
  直接成功、跳过活跃性复查 → 双开(reviewer NEW-ISSUE-C)。故占位**不写任何 TTL
  属性**;漏释放的锁靠 app 层自愈(_placeholder_stealable 通道①指向租户明确 deleted /
  ②占位过期且租户不活跃)在下次同名 create 时回收,不依赖 DDB sweeper。
- **作用域 scope 取最细可用身份**:tenant_user_id > platform_id > owner_id。api-key
  哨兵 owner(clients.API_KEY_OWNER)下退化为该 owner 作用域。不同作用域(不同客户)
  可同名。
- **api-key 哨兵作用域的信息泄漏抑制**(reviewer #1):多个未带更细身份的 api-key 调用
  方共享 owner=API_KEY_OWNER 同一作用域,同名会 409,但 409 body **不回 existing
  tenant_id**(否则跨调用方探测 oracle)。带更细身份(platform_id/tenant_user_id)的
  作用域不共享,可回 id 方便定位。
- scope/name 任一为空 → 跳过去重(fail-open,不改变匿名/无名创建的现有行为)。
"""

import time

from botocore.exceptions import ClientError

import core.clients as clients
import core.utils as utils

# 占位新鲜窗口:短于此视为在途/活跃赢家,绝不抢占;超过视为僵尸可抢占。对齐
# inflight/health_check 的 30min creating 超时——一个 create 最长在途 ~30min,超过
# 必已终态(running/failed/deleted),占位若还在就是漏释放的僵尸。
_LOCK_TTL_SECONDS = 1800

# tenant 记录**明确** deleted 时,允许在 TTL 内提前抢占(软删后立即重建同名)。
# 注意:只认"记录在且 status=deleted",不认"记录不存在"(后者可能是赢家还没写)。
_DELETED_STATE = "deleted"

# 活跃状态集(过 TTL 抢占分支复查用):占位指向的 tenant 属这些状态 = 仍活跃,即便
# 占位按年龄算过期也不可抢(reviewer NEW-ISSUE-A:running >30min 的活租户占位年龄过期)。
_ACTIVE_STATES = frozenset(
    {"creating", "pending", "running", "stopped", "stopping", "starting", "deleting"}
)


def dedup_scope(owner_id, tenant_user_id="", platform_id=""):
    """去重作用域:取最细可用身份(tenant_user_id > platform_id > owner_id).

    api-key 哨兵 owner(clients.API_KEY_OWNER)下若无 tenant_user_id/platform_id,
    退化为该 owner 作用域——即"该 api-key 客户内 name 唯一"。全空返回 ""(跳过去重)。
    """
    return (
        (tenant_user_id or "").strip()
        or (platform_id or "").strip()
        or (owner_id or "").strip()
    )


def _lock_key(scope: str, name: str) -> str:
    """占位主键。不与正常 tenant id(name-xxxx / t-xxxx)或 inflight 锁碰撞."""
    return f"activename#{scope}#{name}"


def _tenant_confirmed_deleted(tenant_id: str) -> bool:
    """占位指向的 tenant 记录**在且明确 deleted**(允许 TTL 内提前抢占).

    只认"记录存在且 status=deleted";记录不存在返回 False(可能是并发赢家还没
    put_item,不能误判僵尸——这正是 reviewer #2 双开的根因)。ConsistentRead。
    """
    if not tenant_id:
        return False
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    return bool(item) and item.get("status") == _DELETED_STATE


def _tenant_is_active(tenant_id: str) -> bool:
    """占位指向的 tenant 是否仍活跃(记录在且 status 属活跃集).ConsistentRead。

    仅在**占位已过 TTL** 的抢占分支用——彼时任何成功的 create 早已完成、赢家记录
    必然已写,所以"记录不存在"= 真僵尸(可抢),不会是 reviewer #2 的"赢家未写"
    假僵尸(那只发生在 TTL 内的新鲜占位,不走这条)。
    """
    if not tenant_id:
        return False
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    return bool(item) and item.get("status") in _ACTIVE_STATES


def _placeholder_stealable(existing: dict) -> bool:
    """现有占位是否可抢占。两条独立通道:
    ① 占位指向的 tenant 记录**明确 deleted**(软删)→ 立即可抢(不必等 TTL)。
    ② 占位 created_at 超 _LOCK_TTL(僵尸/漏释放/进程崩)**且指向的 tenant 已不活跃**
       → 可抢。**过 TTL 也要复查活跃性**(reviewer NEW-ISSUE-A):占位 created_at 首建
       后永不刷新,一个 running >30min 的活租户占位"按年龄算过期"却仍活着;若只看年龄
       盲抢会抢掉活租户的 name → 双开。过 TTL 后复查是安全的:此时赢家记录必已写,
       记录不存在=真僵尸(可抢),不会误判在途赢家。
    新鲜占位(赢家在途,TTL 内且租户非明确 deleted)→ 不可抢(409)。
    """
    tid = existing.get("locked_tenant_id", "")
    # 通道①:明确 deleted → 立即可抢(软删后立即重建同名)。
    if _tenant_confirmed_deleted(tid):
        return True
    # 通道②:占位过期 且 指向租户已不活跃(deleted/不存在)→ 僵尸,可抢。
    created_at = existing.get("created_at", "")
    try:
        import datetime as _dt

        ts = _dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_ok = (time.time() - ts.timestamp()) > _LOCK_TTL_SECONDS
    except Exception:  # noqa: BLE001 — 解析失败保守不抢(fail-closed 到"不双开")
        return False
    return age_ok and not _tenant_is_active(tid)


def acquire_name_lock(scope, name, tenant_id):
    """占位 (scope, name)。成功返回 None;同作用域已有**活跃**同名租户返回
    (409_resp, existing_tenant_id)。

    并发正确性(reviewer #2 修复):以占位自身年龄判活跃,不查赢家 tenant 记录是否
    存在。新鲜占位 → 赢家在途 → 409,绝不抢占(杜绝"赢家记录还没写→输家误判僵尸→
    双开")。僵尸(占位过期 或 指向租户明确 deleted)→ 抢占(允许软删后建同名 +
    进程崩溃/漏释放自愈)。

    scope/name 任一为空 → 跳过(返回 None,不去重)。
    """
    if not scope or not name:
        return None

    lock_id = _lock_key(scope, name)
    now = utils._now()
    # api-key 哨兵共享作用域:409 不泄漏 existing tenant_id(防跨调用方探测 oracle)。
    hide_existing = scope == clients.API_KEY_OWNER

    def _err_dup(existing_tid):
        shown = "" if hide_existing else existing_tid
        return (
            utils._err(
                409,
                "NAME_EXISTS",
                f"a tenant named '{name}' already exists for this owner"
                f"{(' (id=' + shown + ')') if shown else ''}",
                extra={"existing_tenant_id": shown} if shown else None,
            ),
            existing_tid,
        )

    def _new_item():
        return {
            "id": lock_id,
            "status": "name_lock",
            "name_lock_scope": scope,
            "name_lock_name": name,
            "locked_tenant_id": tenant_id,
            "created_at": now,
            # 刻意不写 inflight_ttl/任何 TTL:生命周期作用域锁不能被 DDB sweeper 盲删
            # (NEW-ISSUE-C)。漏释放靠 _placeholder_stealable app 层自愈回收。
        }

    try:
        clients.tenants_table.put_item(
            Item=_new_item(),
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # 占位已存在:看它是否可抢占(过期僵尸 / 指向租户明确 deleted)。
        existing = clients.tenants_table.get_item(
            Key={"id": lock_id}, ConsistentRead=True
        ).get("Item", {})
        existing_tid = existing.get("locked_tenant_id", "")
        if not _placeholder_stealable(existing):
            # 新鲜占位 = 赢家在途/活跃 → 409,绝不抢占。
            return _err_dup(existing_tid)
        # 僵尸 → 抢占。条件写限定占位仍是我们刚读到的那个(locked_tenant_id +
        # created_at 双限定),防两个并发抢占互相覆盖:输家 CCF → 保守 409 让其重试。
        try:
            clients.tenants_table.update_item(
                Key={"id": lock_id},
                UpdateExpression="SET locked_tenant_id = :new, created_at = :t",
                ConditionExpression=(
                    "locked_tenant_id = :old AND created_at = :old_ca"
                ),
                ExpressionAttributeValues={
                    ":new": tenant_id,
                    ":old": existing_tid,
                    ":old_ca": existing.get("created_at", ""),
                    ":t": now,
                },
            )
        except ClientError as e2:
            if (
                e2.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                # 抢占竞争输家:占位已被并发者翻新(现在新鲜)→ 保守 409 让客户重试。
                return _err_dup(existing_tid)
            raise
    return None


def release_name_lock(scope, name, tenant_id=""):
    """释放占位(delete 达终态 / 创建失败回滚时调).best-effort。

    条件写限定占位仍指向本 tenant_id,避免误删"已被同名新租户抢占的"占位
    (软删后立刻重建同名的竞态:老租户 delete 释放时,新占位可能已指向新租户)。
    tenant_id 为空则无条件删(向后兼容 / 调用方不关心)。
    真错误(非 CCF)fail-loud 打印(僵尸自愈兜底,但不静默吞)。
    """
    if not scope or not name:
        return
    lock_id = _lock_key(scope, name)
    try:
        if tenant_id:
            clients.tenants_table.delete_item(
                Key={"id": lock_id},
                ConditionExpression="locked_tenant_id = :tid",
                ExpressionAttributeValues={":tid": tenant_id},
            )
        else:
            clients.tenants_table.delete_item(Key={"id": lock_id})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            # CCF = 占位已被别的租户抢占(不该删),正常;其它是真错误 → fail-loud
            # (app 层僵尸自愈兜底——下次同名 create 抢占,不阻塞 delete,但不静默)。
            print(f"[#240] release_name_lock({lock_id}) non-fatal error: {e}")
    except Exception as e:  # noqa: BLE001 — best-effort,不阻塞 delete;但打印不静默
        print(f"[#240] release_name_lock({lock_id}) unexpected: {e}")
