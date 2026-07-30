# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/ssm_dispatch — VM 生命周期 SSM 下发(handler-split #132 Phase1 T1.3)。

从 handler.py 机械搬迁,函数体逐字不变:_launch_vm_wake_cmd / _launch_vm /
_ssm_send / _ssm_run。_ssm_run 被 14 处调用(facade 别名保持 handler.<sym> 可用)。
共享 ssm client 从 core.clients import;stdlib(shlex/time) 本模块自带。
#187 P5: 原  Cognito b64 分支(base64/json)已随 channel/hub 下线一并移除,
第 11 位保留空占位("")保持位置对齐。
按 design.md 层间契约:core 域不反向 import services/routes。
facade:handler.py re-export 全部符号,旧 patch/调用路径全程有效。
"""

import shlex
import time

from core.clients import ssm


def _launch_vm_wake_cmd(tenant_id, item):
    """#41 — 唤醒/重启/reset/rebuild 用的 launch-vm.sh 完整命令(带全部 11 位参数)。

    老版本这四条路径生成的裸 `launch-vm.sh <tid> <vmn> <vcpu> <mem>` 只填 4 个位置参,
    第 10 位 CHAT_EP_ENABLED 恒空 → launch-vm 幂等段拿 "" 走 no-op → 但由于 launch-vm
    老版本把 chatCompletions 收敛塞在 NEW_DATA-only,唤醒本就跳过,配置漂移根本无从修复。
    #41 把幂等段抽出后,幂等段需要 CHAT_EP_ENABLED 显式的 "1"/"0" 才能正确开/关;这里
    从 tenant record 穿透该 flag,让 restart/start/reset/rebuild 都能带上租户当前的开关值。
    幂等段其它三态(空/未知)按 fail-safe 不动数据盘上的现有 chatCompletions。

    apiKey / channel_secret 唤醒路径的语义:
      • LITELLM_VKEY(第 8 位):留空,让 launch-vm 走 shared fallback 或保留数据盘现值。
        DDB record 里存的是"per-tenant vkey 值",但 wake 场景不覆盖数据盘上已铸的 key
        是最安全的(harden-config 参数空 → 不写 apiKey),空参也不会误改。
      • INJECTED_CHANNEL_SECRET(第 9 位):留空。数据盘上的 channel_secret 是首次铸的,
        NEW_DATA 分支保护它不被重跑覆盖(参数进 NEW_DATA-only 分支才用);wake 传空无影响。
      • 第 11 位保留空占位(#187 P5 前是 INJECTED_COGNITO_B64,已下线);位置对齐给
        launch-vm.sh 幂等段。
    只有 CHAT_EP_ENABLED 需要显式穿透——因为它是可切换的 per-tenant 开关,唤醒场景
    应该收敛到当前 DDB 值,而不是 fail-safe 保留旧盘值(那会误关新开的租户)。
    """
    vm_num = int(item.get("vm_num", 1))
    vcpu = int(item["vcpu"])
    mem_mb = int(item["mem_mb"])
    # 前 7 位空占位(config_template / restore / scoped_skills 唤醒不重下)——launch-vm
    # 自己 special-case `""` 保留位置对齐(launch-vm.sh:75)。8/9/11 空位同理。
    # 10 位 CHAT_EP_ENABLED 从 record 读:record 只有 chat_endpoint_enabled=True 才存
    # 该字段(handler.py:1972/2126),所以 .get() 拿 True/None,转 "1"/"0"。
    cee = bool(item.get("chat_endpoint_enabled", False))
    chat_ep_arg = "1" if cee else "0"
    # #187 P1: position 12 (gateway_token_ct) is intentionally OMITTED on wake —
    # NEW_DATA=false so the openclaw.json guard block doesn't run, and the token
    # baked into the data disk on first launch is authoritative. Also the reveal
    # window has already closed by wake time (>15min after create). launch-vm.sh
    # reads it as `${12:-}` → "" → no-op, safe.
    # #188: position 13 (device_paired_b64) is likewise OMITTED on wake — the
    # paired.json cold-injected into the data disk on first launch persists (it
    # lives under the data disk's <stateDir>/devices/, not regenerated). NEW_DATA
    # guards the write so wake never re-injects. launch-vm.sh reads `${13:-}` →
    # "" → skips the paired.json write, safe.
    return (
        f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} "
        f'"" "" "" "" "" {chat_ep_arg} "" "" ""'
    )


def _launch_vm(
    instance_id,
    tenant_id,
    vm_num,
    vcpu,
    mem_mb,
    guest_ip,
    host_port,
    config_template="",
    restore_backup_key="",
    scoped_skills=None,
    litellm_vkey="",
    channel_secret="",
    chat_endpoint_enabled=False,
    gateway_token_ct=None,
    device_paired_b64="",
):
    """Fire-and-forget: launch VM + set up DNAT.

    If restore_backup_key is non-empty, launch-vm.sh will restore data.ext4 from that S3 key instead of using the template.

    1.4.0 (#62): scoped_skills is None or [] for the legacy "broadcast"
    behavior, or a list of skill names for per-tenant scoping. Passed as
    a comma-separated string to launch-vm.sh as the 7th positional arg
    (empty string == broadcast, comma-list == only those subdirs cp'd).
    """

    # issue #59 (WI-E/M-1) — every caller/external-influenced string below is
    # interpolated into an SSM AWS-RunShellScript command that runs as ROOT on a
    # shared host. Shell-quote each so a value can never break out of its
    # positional argument (defense in depth behind create_tenant's input regex).
    # Empty → the literal "" placeholder launch-vm.sh already special-cases
    # (launch-vm.sh:75) so positional alignment is preserved.
    def _q(val):
        return shlex.quote(val) if val else '""'

    # When restore is used but no template, still need a placeholder in arg 5 so positional args align.
    tpl_arg = _q(config_template)
    # Placeholder for restore_backup_key (arg 6) so arg 7 always lines up.
    restore_arg = _q(restore_backup_key)
    # 1.4.0: 7th positional arg — comma-separated skill list (or empty for broadcast).
    skills_arg = _q(",".join(scoped_skills)) if scoped_skills else '""'
    # task #15: 8th positional arg — per-tenant LiteLLM vkey (empty → shared key).
    vkey_arg = _q(litellm_vkey)
    # 9th positional arg — control-plane-minted channel_secret (hub HMAC). Empty
    # → launch-vm.sh falls back to generating its own (legacy; reintroduces the
    # host-agent read-back race). Non-empty (normal path) → DDB & guest share it.
    csecret_arg = _q(channel_secret)
    # 10th positional arg — per-tenant chatCompletions switch. Default off ("0")
    # keeps launch-vm.sh deleting the endpoint (secure default; see the ops guide
    # "chatCompletions 为什么不能全局默认开"). Only tenants with
    # chat_endpoint_enabled=true in DDB get "1" → enabled:true injected.
    chat_ep_arg = "1" if chat_endpoint_enabled else "0"
    # #187 P5: 11th positional arg — 保留空占位。转型前是 INJECTED_COGNITO_B64
    # ( 端到端 Cognito 渠道机器用户 base64),随 channel/hub 一起下线;这里
    # 传 "" 保持位置对齐,launch-vm.sh 位置参解析不动。
    cognito_arg = '""'
    # #187 P1: 12th positional arg — base64 ciphertext of the pre-minted gateway
    # token (tenant_id EncryptionContext, ClawPool CMK). Empty ("") when the CMK
    # feature is off — launch-vm.sh keeps `openssl rand`ing its own token in-VM
    # (byte-identical pre-#187 behavior for un-migrated deployments). The
    # ciphertext is already base64 text from core.kms_envelope.encrypt_with_tenant,
    # so no re-encoding — shell-quote it defensively even though base64url only
    # produces [A-Za-z0-9_-] (belt-and-suspenders behind the input validation
    # regex on tenant creation).
    gw_token_arg = _q(gateway_token_ct) if gateway_token_ct else '""'
    # #188: 13th positional arg — base64 of the paired.json entry (one Ed25519
    # device: deviceId + publicKey + roles + scopes, tokens:{} for 2026.2.26).
    # launch-vm.sh base64-decodes it and writes <stateDir>/devices/paired.json so
    # a remote WSS client (JDWS) preloaded with the matching device identity
    # connects to the in-VM gateway with NO manual approve. Empty ("") when no
    # device was minted (owner unknown / CMK off) → launch-vm.sh skips the write
    # (byte-identical pre-#188 behavior; feature off = no paired.json). The value
    # is already base64 text from create_tenant, shell-quote defensively.
    device_paired_arg = _q(device_paired_b64) if device_paired_b64 else '""'
    # The live two-tier route is allocated by host-agent from the configured
    # bitmap range and committed to Redis after the guest passes health checks.
    # The historical VM_PORT_BASE+vm_num rule was never consumed by edge, was
    # outside the edge->host SG range, and could not be reconstructed from DDB
    # after host-agent replaced host_port during promotion. Do not create that
    # second, permanently leaked DNAT family.
    cmd = (
        f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} "
        f"{tpl_arg} {restore_arg} {skills_arg} {vkey_arg} {csecret_arg} "
        f"{chat_ep_arg} {cognito_arg} {gw_token_arg} {device_paired_arg}"
    )
    # Return the SSM CommandId (or None if submission failed — notably an SSM
    # SendCommand ThrottlingException under concurrent consumer fan-out, loop
    # 2026-07-01 real-machine bug). Callers on the create path check this: a
    # None means launch-vm never ran, so the tenant must be rolled back and the
    # create retried, not left stuck in `creating` with a leaked capacity slot.
    return _ssm_send(instance_id, cmd, timeout=300)


def _ssm_send(instance_id, command, timeout=120):
    """Fire-and-forget SSM command. Returns the CommandId (str) so callers can
    later poll get_command_invocation for completion, or None if submission
    failed. Existing call sites that ignore the return value are unaffected;
    the async migrate path (issue #64) stores the CommandId in DynamoDB so the
    health_check sweep can advance the migration out-of-band."""
    try:
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        return resp["Command"]["CommandId"]
    except Exception as e:
        print(f"SSM send error: {e}")
        return None


def _ssm_run(instance_id, command, timeout=30):
    """Execute command on host via SSM Run Command. Returns True on success."""
    try:
        # SSM runs as root; set HOME so ~ resolves to /home/ubuntu
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(3)  # Wait for invocation to register
        for _ in range(timeout // 2):
            try:
                result = ssm.get_command_invocation(
                    CommandId=cmd_id,
                    InstanceId=instance_id,
                )
                status = result["Status"]
                if status == "Success":
                    return True
                if status in ("Failed", "TimedOut", "Cancelled"):
                    print(
                        f"SSM failed: {status} - {result.get('StandardErrorContent', '')}"
                    )
                    return False
            except ssm.exceptions.InvocationDoesNotExist:
                pass
            time.sleep(2)
        print(f"SSM timeout waiting for command {cmd_id}")
        return False
    except Exception as e:
        print(f"SSM error: {e}")
        return False
