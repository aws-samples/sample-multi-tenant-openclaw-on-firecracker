# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#394 step5 — 同步槽位操作:promote-canary / cleanup canary / reclaim-images。
(rollback-image 已撤销:回滚 = pull 老版到 live,本地已完整则快路径秒级翻指针。)

为什么独立成模块而不塞进 host_service:host_service.py 已 1700+ 行(远超本仓 800 行硬上限),
按 code-craft 规则"改已超限老文件时新代码不再往里堆",故新增面单独落这里。

三个操作的共同形状(ADR §4.5/§4.6/§4.7/§4.9):
  · **同步**:只改 host 上 slots.json 一个小文件(不搬盘),秒级返回,不给调用方轮询负担;
  · **CAS**:调用方必须带它读到过的 expected snapshot_time + generation,不符即拒 —— 这才
    能保证"验证的版本 == 提升的版本";
  · **持 image lease**:与 pull 抢同一把 host 级锁,防"pull 正在装 canary 时 promote 抢跑";
  · **幂等**:live 已是目标版本 → already_promoted;canary 已空 → cleanup 幂等成功。

槽位真值在 host 本地 slots.json(ADR §4.2),故读改写都经 SSM 在 host 上做;控制面只做
准入、CAS 判定与结果记录。
"""

import json
import os
import shlex
import time
import uuid

from core import image_lease, image_ops, image_slots
from core.clients import hosts_table, tenants_table
from core.utils import _err, _now, _resp

# host slots-write 脚本 fence 门要读 DDB(强一致)确认 lease owner/epoch,需 region + 表名。
_REGION = os.environ.get("AWS_REGION", "") or os.environ.get("AWS_DEFAULT_REGION", "")
_HOSTS_TABLE_NAME = os.environ.get("HOSTS_TABLE", "openclaw-hosts")

# SSM 同步操作的等待上限:只改一个小文件,正常 <5s;给 60s 冗余(SSM 下发本身有排队)。
# 超时不代表失败(可能已提交),故超时返回 503 OPERATION_STATUS_UNKNOWN 让调用方带同
# Idempotency-Key 重试对账(ADR §4.9),绝不谎报成功或失败。
_SYNC_TIMEOUT_S = 60


def _read_slots(instance_id, ssm_wait):
    """读 host 上的 slots.json(本地真值)。返回 (slots_dict, err_response)。"""
    q = shlex.quote
    script = f"cat {q(image_slots.SLOTS_FILE)} 2>/dev/null || echo '__NO_SLOTS__'"
    _cmd, ok, out = ssm_wait(instance_id, script, timeout=_SYNC_TIMEOUT_S)
    if not ok:
        return None, _err(
            503, "DEPENDENCY_UNAVAILABLE",
            f"cannot read slots.json on host {instance_id} (SSM failed)",
        )
    raw = (out or "").strip()
    if not raw or "__NO_SLOTS__" in raw:
        # 还没导入版本目录的老 host:没有 slots.json。此时没有 canary 可提升/无锚点可回滚,
        # 交由各操作按空 slots 判定(promote → CANARY_CHANGED、rollback → NO_PREVIOUS_LIVE)。
        return image_slots.empty_slots(), None
    try:
        return image_slots.normalize(raw), None
    except (ValueError, json.JSONDecodeError) as e:
        # 损坏必须 fail-loud:当空处理会让 promote 以为"没有 canary"、rollback 丢锚点。
        return None, _err(
            500, "SLOTS_CORRUPT",
            f"slots.json on host {instance_id} is unparseable: {e}",
        )


def _write_slots(instance_id, new_slots, ssm_wait, op_id=None, fence_epoch=None):
    """把新 slots 原子写回 host(同 core.image_slots 的提交协议)+ 回写 DDB 镜像。返回 err|None。

    (flock + 强一致读 DDB 校验 owner==op_id 且 epoch 未被接管递增),挡"超时旧 SSM 命令晚到
    覆盖新操作"。这道门必须在 host 侧(Lambda 侧 fence_valid 拦不住已下发的命令)。
    """
    lines = ["set -eu", '_perr() { echo "ERROR:$*" >&2; }']
    if op_id is not None and fence_epoch is not None:
        lines += image_slots.slots_fence_guard_lines(
            _HOSTS_TABLE_NAME, _REGION, instance_id, op_id, fence_epoch
        )
    lines += image_slots.slots_write_lines(new_slots)
    _cmd, ok, out = ssm_wait(instance_id, "\n".join(lines), timeout=_SYNC_TIMEOUT_S)
    if not ok:
        # 关键:写命令失败/超时【不能】断言"没提交"—— 可能已 rename 成功但回执丢了。
        # 返回 OPERATION_STATUS_UNKNOWN,调用方用同 Idempotency-Key 重试对账(ADR §4.9)。
        return _err(
            503, "OPERATION_STATUS_UNKNOWN",
            f"slots.json update on host {instance_id} did not confirm; retry with the "
            f"same Idempotency-Key to reconcile. detail={(out or '')[:200]}",
        )
    # promote/rollback/cleanup 改了 host 真值,镜像不跟就会让下次 promote 误报 CANARY_CHANGED、
    # UI 显示旧值)。sync 操作里 Lambda 手握 new_slots 权威值,直接写 DDB 即可(不必绕 host)。
    # best-effort:镜像写失败不推翻已确认的 host 提交(host 是真值,下次操作会再纠)。
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET image_slots = :m, image_slots_synced_at_epoch = :ts",
            ExpressionAttributeValues={":m": {
                "live": new_slots.get("live"),
                "canary": new_slots.get("canary"),
                "previous_live": new_slots.get("previous_live"),
                "generation": int(new_slots.get("generation") or 0),
            }, ":ts": int(time.time())},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[slot-op] WARN mirror writeback failed for {instance_id}: {e}")
    return None


def _idempotent_replay(instance_id, idempotency_key, operation, expected=None):
    """同 Idempotency-Key 重放 → 直接返回上次那条结果(ADR §4.9)。

    返回 (response|None, err_response|None)。
    · 已有成功记录 → 原样返回它的 result(同一个答案,调用方能确定"我那次成了");
    · 记录还在 IN_PROGRESS → 409 IMAGE_OPERATION_IN_PROGRESS(别并发跑第二遍);
    · 同 key 用在了别的操作、或【同操作但请求体不同】→ 409 IDEMPOTENCY_KEY_REUSED
      (调用方 bug,不能拿 promote A 的结果去答 promote B)。
    """
    if not idempotency_key:
        return None, None
    prior = image_ops.find_by_idempotency_key(instance_id, idempotency_key, operation)
    if not prior:
        return None, None
    if prior.get("operation") != operation:
        return None, _err(
            409, "IDEMPOTENCY_KEY_REUSED",
            f"Idempotency-Key was already used for {prior.get('operation')!r}; "
            f"use a fresh key per business operation",
        )
    # snapshot/generation = 不同请求,报 KEY_REUSED,绝不拿旧请求的结果冒充。
    if expected is not None and (prior.get("expected") or {}) != expected:
        return None, _err(
            409, "IDEMPOTENCY_KEY_REUSED",
            f"Idempotency-Key already used for {operation} with different parameters "
            f"({prior.get('expected')}); this request has {expected}. Use a fresh key.",
        )
    state = prior.get("state")
    if state == image_ops.STATE_IN_PROGRESS:
        # 看这条 op 是否仍持 image lease。_with_lease 在 finally 里【一定】归还 lease,所以
        # 如果 intent 是 IN_PROGRESS 但该 op 已不再是 lease 持有者 → 它其实已经跑完(record_result
        # 遇 DDB 瞬时故障没落终态)。此时【重跑对账】而不是永久 409(promote/cleanup/reclaim 幂等,
        # 已提交则收敛 already_*/已空/no-op)。仍持 lease = 真的在并发执行 → 保持 409 让稍后重试。
        op_id = prior.get("job_id")
        lease = image_lease.read(instance_id)
        # owner 字段在 lease 过期后仍会保留；只有 owner 匹配且租约仍在有效期内，
        # 才代表同一次操作真的还在执行。否则按 stale IN_PROGRESS 重跑对账。
        still_owner = (
            bool(lease)
            and lease.get("active_image_operation_id") == op_id
            and image_lease.is_held(lease)
        )
        if still_owner:
            return None, _err(
                409, "IMAGE_OPERATION_IN_PROGRESS",
                "the same Idempotency-Key is still executing; retry shortly",
            )
        # lease 已释放 → 陈旧 IN_PROGRESS,重跑对账(等同 UNKNOWN)。
        return None, None
    if state == image_ops.STATE_SUCCEEDED:
        return _resp(200, dict(prior.get("result") or {}, replayed=True)), None
    if state == image_ops.STATE_UNKNOWN:
        # 返回 (None, None) 让上层【重新执行】对账。promote/cleanup/reclaim 都幂等,已提交则
        # 再跑收敛(already_promoted / 已空 / no-op),未提交则真正做完。这才兑现"503 后同
        # Idempotency-Key 安全重试对账"的文档承诺。
        return None, None
    return None, _err(
        409, "OPERATION_FAILED_PREVIOUSLY",
        f"the same Idempotency-Key previously failed: {prior.get('error')}; "
        f"use a fresh key to retry",
    )


def _with_lease(instance_id, ssm_wait, fn, operation=None,
                idempotency_key=None, expected=None):
    """幂等重放检查 → 抢 image lease → 跑 fn(slots) → 记结果 → 归还 lease。

    与 pull 抢同一把锁:防"canary 正在装(pull 持锁)时 promote 抢跑"读到半装的槽位。
    finally 归还,任何早退/异常都不泄漏锁(泄漏 = 该 host 镜像操作被占死到自然过期)。
    intent 在【拿到锁之后、发 host 命令之前】落库(ADR §4.9);result 在 fn 返回后落库。
    """
    replay, replay_err = _idempotent_replay(
        instance_id, idempotency_key, operation, expected
    )
    if replay_err:
        return replay_err
    if replay:
        return replay
    op_id = f"op-{uuid.uuid4().hex[:16]}"
    epoch, lease_err = image_lease.acquire(instance_id, op_id, "slot-op")
    if lease_err:
        return _err(409, "IMAGE_OPERATION_IN_PROGRESS", lease_err)
    try:
        image_ops.record_intent(op_id, instance_id, operation, expected, idempotency_key)
        slots, err = _read_slots(instance_id, ssm_wait)
        if err:
            image_ops.record_result(op_id, False, error={"stage": "read-slots"})
            return err
        resp = fn(slots, op_id, epoch)
        ok = resp.get("statusCode") == 200
        body = {}
        try:
            body = json.loads(resp.get("body") or "{}")
        except (ValueError, TypeError):
            body = {}
        #  · 200 → SUCCEEDED(重放返回该 result);
        #  · 503 OPERATION_STATUS_UNKNOWN(host 命令超时/回执丢,可能已提交)→ UNKNOWN,
        #    绝不落 FAILED(否则同 key 重试被 409 挡死,违背"503 后重试对账"承诺);重放会重跑对账;
        #  · 其它非 200(真拒绝:409 CANARY_CHANGED / 400 等)→ FAILED,同 key 重试报
        #    OPERATION_FAILED_PREVIOUSLY(拿新 key 重试)。
        unknown = (resp.get("statusCode") == 503
                   and body.get("code") == "OPERATION_STATUS_UNKNOWN")
        if ok:
            image_ops.record_result(op_id, True, result=body)
        elif unknown:
            image_ops.record_result(op_id, False, error={"body": body},
                                    state=image_ops.STATE_UNKNOWN)
        else:
            image_ops.record_result(op_id, False, error={"body": body})
        return resp
    finally:
        image_lease.release(instance_id, op_id)


def _host_exists(instance_id):
    return bool(
        hosts_table.get_item(
            Key={"instance_id": instance_id}, ConsistentRead=True
        ).get("Item")
    )


def _pinned_versions_on_host(instance_id):
    """该 host 上所有【非 deleted】租户仍固定引用的 image_snapshot_time 集合(reclaim 保护名单)。

    reclaim 要一次性算出"哪些版本还有人用",漏一个就会把在用底盘删掉(no-data-loss)。
    故【必须翻页翻到底】:scan 一页 1MB 上限,漏读一页 = 少保护一批版本 = 误删。
    """
    pinned = set()
    kwargs = {
        "FilterExpression": "host_id = :h AND attribute_exists(image_snapshot_time) "
                            "AND #st <> :deleted",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":h": instance_id, ":deleted": "deleted"},
        "ProjectionExpression": "image_snapshot_time",
        # Reclaim is destructive. An eventually consistent page may omit a
        # just-pinned tenant and incorrectly classify its image as orphaned.
        "ConsistentRead": True,
    }
    while True:
        resp = tenants_table.scan(**kwargs)
        for it in resp.get("Items") or []:
            v = it.get("image_snapshot_time")
            if v:
                pinned.add(v)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return pinned
        kwargs["ExclusiveStartKey"] = lek


def _immutable_present_in_snapshot(snapshot_time):
    """#534 F2 —— promoted 快照是否带只读盘。读 version-snapshots 存的 files,复用
    host_service._select_pull_files 读该快照 manifest 判定(其第 4 返回值 immutable_version
    非空 ⟺ manifest 点名了 immutable 盘)。返回 True/False;判不了(表未配 / 无 files /
    manifest 读失败)返 None → 调用方保守:不动 immutable_version(既不谎报存在、也不误清)。"""
    try:
        from core.clients import version_snapshots_table
        if version_snapshots_table is None:
            return None
        item = version_snapshots_table.get_item(
            Key={"snapshot_time": snapshot_time}, ConsistentRead=True
        ).get("Item")
        if not item or not item.get("files"):
            return None
        files = json.loads(item["files"])
        import services.host_service as _hs  # 惰性 import,避免模块级环
        _, err, _, imm_ver = _hs._select_pull_files(os.environ.get("ASSETS_BUCKET", ""), files)
        if err:
            return None
        return bool(imm_ver)
    except Exception:  # noqa: BLE001 —— 判不了就返 None(保守),不让它把 promote 带崩
        return None


def _sync_host_version_coordinate(instance_id, live_snapshot):
    """#534 —— promote 翻 live 后把 host 版本坐标同步成 live 快照的 label(B2:host.rootfs_version /
    immutable_version 恒等于其 image_slots.live 的 label)。
    - rootfs_version:总盖(rootfs 盘必存)。
    - immutable_version(F2):仅当该快照【确带只读盘】才盖;确认【不带】→ REMOVE(别谎报);
      判不了(None)→ 不动(保守)。
    成功返 None;写失败返错误串(F3:调用方据此返回可重试 unknown/error,绝不吞错仍返 200 ——
    否则 host 坐标永久停旧且无人知)。反解 snapshot_time→label(查不到透传,fail-safe)。"""
    from core.version_labels import label_for_snapshot
    label = label_for_snapshot(live_snapshot)
    has_imm = _immutable_present_in_snapshot(live_snapshot)
    expr = "SET rootfs_version = :v"
    if has_imm is True:
        expr += ", immutable_version = :v"
    elif has_imm is False:
        expr += " REMOVE immutable_version"
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=expr,
            ExpressionAttributeValues={":v": label},
            ConditionExpression="attribute_exists(instance_id)",
        )
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def promote_canary(instance_id, body, ssm_wait, headers=None):
    """POST /hosts/{id}/promote-canary —— canary 槽升为 live(同步,ADR §4.5)。"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    if not _host_exists(instance_id):
        return _err(404, "NOT_FOUND", f"host {instance_id} not found")
    payload = _parse_body(body)
    if payload is None:
        return _err(400, "VALIDATION", "body must be a JSON object")
    expected_snap = (payload.get("expected_canary_snapshot_time") or "").strip()
    if not expected_snap:
        return _err(
            400, "VALIDATION",
            "expected_canary_snapshot_time required (CAS: proves you promote the version you verified)",
        )
    expected_gen = payload.get("expected_canary_generation")

    def _do(slots, _op_id, _epoch):
        new_slots, already, err = image_slots.apply_promote(
            slots, expected_snap, expected_gen
        )
        if err:
            return _err(
                409, err,
                f"canary on host {instance_id} is {slots.get('canary')!r} at generation "
                f"{slots.get('generation')}, not {expected_snap!r} at {expected_gen} — "
                f"re-read and retry (never promote a version you did not verify)",
            )
        if already:
            # #534 F3 —— already_promoted 路径【也】同步 host 坐标(重试补偿:第一次 promote 翻了
            # live 但坐标那步失败,重试会走到这里补齐)。同步失败返回可重试,不谎报成功。
            _serr = _sync_host_version_coordinate(instance_id, new_slots.get("live"))
            if _serr:
                return _err(
                    503, "COORDINATE_SYNC_FAILED",
                    f"canary already live but host version-coordinate sync failed ({_serr}); retry to reconcile",
                )
            return _resp(200, {
                "message": "canary already promoted to live",
                "instance_id": instance_id, "already_promoted": True,
                "live_snapshot_time": new_slots.get("live"),
                "generation": new_slots.get("generation"),
            })
        werr = _write_slots(instance_id, new_slots, ssm_wait, _op_id, _epoch)
        if werr:
            return werr
        # #534 §9 + F2/F3 —— 翻 live 后同步 host 版本坐标 = 新 live 的 label(B2:host.rootfs_version /
        # immutable_version 恒等于其 image_slots.live 的 label);此前 promote 只翻 slots 指针、不动坐标
        # → promote 后 host 已跑新版但坐标停旧、新建租户继承旧值、drift/stats 看不见。
        # F3:同步失败【返回可重试】,不吞错仍返 200(否则坐标永久停旧且无人知);重试走上面
        # already_promoted 分支会再同步一次(幂等收敛)。F2:immutable_version 仅当快照确带只读盘才盖。
        _serr = _sync_host_version_coordinate(instance_id, new_slots["live"])
        if _serr:
            return _err(
                503, "COORDINATE_SYNC_FAILED",
                f"canary promoted to live but host version-coordinate sync failed ({_serr}); retry to reconcile",
            )
        return _resp(200, {
            "message": "canary promoted to live",
            "instance_id": instance_id, "already_promoted": False,
            "live_snapshot_time": new_slots["live"],
            "previous_live_snapshot_time": new_slots["previous_live"],
            "generation": new_slots["generation"],
            "promoted_at": _now(),
        })

    return _with_lease(
        instance_id, ssm_wait, _do, operation="promote-canary",
        idempotency_key=_idempotency_key(headers),
        expected={"canary": expected_snap, "generation": expected_gen},
    )


# 翻指针,零下载)。不再需要独立的 live↔previous_live swap 操作(它的 swap 语义反直觉、且与
# "选定版本重指"的行业模型不一致)。previous_live 槽仍作纯展示信息保留(不再有 swap 动作)。


# 无需显式清指针——下次 `pull-image?slot=canary` 覆盖该槽,promote 成功也会清空它;磁盘回收由
# `reclaim-images` 承担(它保护 {live,canary,previous_live}∪租户固定版本,其余版本目录才删)。


def reclaim_images(instance_id, headers, ssm_wait):
    """POST /hosts/{id}/reclaim-images —— 回收该 host 上无人引用的版本目录(手动 prune,#394)。

    cleanup-canary 只清指针不删盘、把物理回收挂在"后台 GC"上,但镜像版本目录没有后台 GC
    (host-agent 的 disk-gc 只收租户 VM 目录),于是丢弃的 canary / 被 promote 顶下来的旧
    previous_live 会永久占盘。本接口是那个回收的显式手动面:保留 {live, canary, previous_live}
    + 所有非 deleted 租户仍固定引用的版本,其余版本目录 rm -rf。

    保护名单在【控制面】算(host 看不到 DDB 租户固定关系),作为显式白名单传给 host,host 只
    做减法。持 image lease 防与 pull 并发(pull 装到一半的 canary 目录还没进 slots.json,不能
    被当孤儿删)。同步、admin-only(不可逆删盘)。
    """
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    if not _host_exists(instance_id):
        return _err(404, "NOT_FOUND", f"host {instance_id} not found")

    def _do(slots, _op_id, _epoch):
        # 前置守卫:没有 live 一律拒绝回收。无 live = host 处于扁平/损坏态(slots.json 缺失或
        # live 未解析),此时 keep-set 少了最关键的一项,prune 可能把该机唯一可起的底盘删掉。
        # 宁可拒绝也不在无 live 时删任何东西(先 pull-canary self-heal 出 live 再来)。
        if not slots.get("live"):
            return _err(
                409, "NO_LIVE_VERSION",
                f"host {instance_id} has no live version in slots.json; refusing to reclaim "
                f"(pull an image to establish live first)",
            )
        keep = image_slots.referenced_versions(slots) | _pinned_versions_on_host(instance_id)
        lines = ["set -eu", '_perr() { echo "ERROR:$*" >&2; }']
        # 挡超时旧命令晚到、按过期 keep-set 误删新 pull 刚装的版本目录。
        lines += image_slots.slots_fence_guard_lines(
            _HOSTS_TABLE_NAME, _REGION, instance_id, _op_id, _epoch
        )
        lines += image_slots.reclaim_versions_lines(keep)
        _cmd, ok, out = ssm_wait(instance_id, "\n".join(lines), timeout=_SYNC_TIMEOUT_S)
        if not ok:
            # 删除命令没确认 —— 但 reclaim 是幂等的(白名单减法),重试无害;不谎报成功。
            return _err(
                503, "OPERATION_STATUS_UNKNOWN",
                f"version reclaim on host {instance_id} did not confirm; safe to retry. "
                f"detail={(out or '')[:200]}",
            )
        reclaimed = [ln[len("__RECLAIMED__"):] for ln in (out or "").splitlines()
                     if ln.startswith("__RECLAIMED__")]
        return _resp(200, {
            "message": "reclaimed unreferenced image versions",
            "instance_id": instance_id,
            "kept_versions": sorted(keep, reverse=True),
            "reclaimed_versions": sorted(reclaimed, reverse=True),
            "reclaimed_count": len(reclaimed),
        })

    return _with_lease(
        instance_id, ssm_wait, _do, operation="reclaim-images",
        idempotency_key=_idempotency_key(headers),
    )


def _idempotency_key(headers):
    """取 Idempotency-Key 头(大小写不敏感;缺失 → None,退化成"每次都真跑"的旧语义)。"""
    for key, value in (headers or {}).items():
        if key.lower() == "idempotency-key":
            return (value or "").strip() or None
    return None


def _parse_body(body):
    """请求体 → dict。空体视作 {}(rollback 可不带 expected);非对象 → None(调用方 400)。"""
    if body is None or body == "":
        return {}
    if isinstance(body, dict):
        return body
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# promote 的 CAS 走 body 的 expected_canary_snapshot_time,reclaim 无 CAS,均不需要 If-Match。
