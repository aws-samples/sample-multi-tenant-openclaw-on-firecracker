# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/ssm_dispatch — VM 生命周期 SSM 下发(handler-split #132 Phase1 T1.3)。

从 handler.py 机械搬迁,函数体逐字不变:_launch_vm_wake_cmd / _launch_vm /
_ssm_send / _ssm_run。_ssm_run 被 14 处调用(facade 别名保持 handler.<sym> 可用)。
共享 ssm client 从 core.clients import;stdlib(shlex/time) 本模块自带。
第 11 位保留空占位("")保持位置对齐。
按 design.md 层间契约:core 域不反向 import services/routes。
facade:handler.py re-export 全部符号,旧 patch/调用路径全程有效。
"""

import shlex
import time

import core.skills as skills
from core.clients import ssm


def _launch_vm_wake_cmd(tenant_id, item):
    """#41 — 唤醒/重启/reset/rebuild 用的 launch-vm.sh 完整命令(带全部 11 位参数)。

    老版本这四条路径生成的裸 `launch-vm.sh <tid> <vmn> <vcpu> <mem>` 只填 4 个位置参,
    第 10 位 CHAT_EP_ENABLED 恒空 → launch-vm 幂等段拿 "" 走 no-op → 但由于 launch-vm
    老版本把 chatCompletions 收敛塞在 NEW_DATA-only,唤醒本就跳过,配置漂移根本无从修复。
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
    需要显式穿透的是 CHAT_EP_ENABLED(第 10 位)和 SCOPED_SKILLS(第 7 位):
      • CHAT_EP_ENABLED 是可切换的 per-tenant 开关,唤醒场景应该收敛到当前 DDB 值,
        而不是 fail-safe 保留旧盘值(那会误关新开的租户)。
      • SCOPED_SKILLS 是 no-cross-tenant 边界,传空会被 launch-vm 当成广播(#517 G6,
        见函数体内注释)。
    """
    vm_num = int(item.get("vm_num", 1))
    vcpu = int(item["vcpu"])
    mem_mb = int(item["mem_mb"])
    # 第 5/6 位空占位(config_template / restore 唤醒不重下)——launch-vm 自己
    # special-case `""` 保留位置对齐(launch-vm.sh:75)。8/9/11 空位同理。
    # #517 G6(no-cross-tenant):第 7 位 SCOPED_SKILLS 必须穿透,此前这里传空。
    # 空值被 launch-vm.sh:1079 判成 broadcast → cp -r 整个 /data/shared-skills 进
    # 数据盘,而 cp 不删除 = 永久留盘。于是一个受限租户只要被 restart/start/reset/
    # rebuild 任一动作唤醒过,就越权持有全部共享 skill 且无法回收。create/restore
    # 早就按 effective scope 传了(tenant_service.py:2264/3531 有同款告警注释),
    # 唯独这四条唤醒路径漏掉。
    # 语义与 _launch_vm 的 skills_arg 逐字一致:None/空 → '""'(未配 scope 的租户
    # 继续走广播,legacy 行为不变);有 scope → 逗号分隔列表,走 scoped 分支。
    _scoped = skills._resolve_effective_skills(item)
    skills_arg = shlex.quote(",".join(_scoped)) if _scoped else '""'
    # 10 位 CHAT_EP_ENABLED 从 record 读:record 只有 chat_endpoint_enabled=True 才存
    # 该字段(handler.py:1972/2126),所以 .get() 拿 True/None,转 "1"/"0"。
    cee = bool(item.get("chat_endpoint_enabled", False))
    chat_ep_arg = "1" if cee else "0"
    # NEW_DATA=false so the openclaw.json guard block doesn't run, and the token
    # baked into the data disk on first launch is authoritative. Also the reveal
    # window has already closed by wake time (>15min after create). launch-vm.sh
    # reads it as `${12:-}` → "" → no-op, safe.
    # paired.json cold-injected into the data disk on first launch persists (it
    # lives under the data disk's <stateDir>/devices/, not regenerated). NEW_DATA
    # guards the write so wake never re-injects. launch-vm.sh reads `${13:-}` →
    # "" → skips the paired.json write, safe.
    return (
        f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} "
        f'"" "" {skills_arg} "" "" {chat_ep_arg} "" "" ""'
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
    sync=False,
    sync_timeout=300,
):
    """Fire-and-forget: launch VM + set up DNAT.

    restore 必须确认 launch-vm.sh 真跑成功(返 rc=0)才把租户翻 running,否则 fire-and-forget
    的 CommandId 只证明"提交了",VM 可能没起来(假成功)。默认 sync=False 保持 create/migrate
    的异步语义不变(它们靠 health_check sweep 异步推进,不阻塞 API)。

    If restore_backup_key is non-empty, launch-vm.sh will restore data.ext4 from that S3 key instead of using the template.

    1.4.0 (#62): scoped_skills is None or [] for the legacy "broadcast"
    behavior, or a list of skill names for per-tenant scoping. Passed as
    a comma-separated string to launch-vm.sh as the 7th positional arg
    (empty string == broadcast, comma-list == only those subdirs cp'd).
    """

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
    vkey_arg = _q(litellm_vkey)
    # 9th positional arg — control-plane-minted channel_secret (hub HMAC). Empty
    # → launch-vm.sh falls back to generating its own (legacy; reintroduces the
    # host-agent read-back race). Non-empty (normal path) → DDB & guest share it.
    csecret_arg = _q(channel_secret)
    # 10th positional arg — per-tenant chatCompletions switch. Default off ("0")
    # keeps launch-vm.sh deleting the endpoint (secure default; see CLAUDE.md
    # "chatCompletions 为什么不能全局默认开"). Only tenants with
    # chat_endpoint_enabled=true in DDB get "1" → enabled:true injected.
    chat_ep_arg = "1" if chat_endpoint_enabled else "0"
    # (WI-002 端到端 Cognito 渠道机器用户 base64),随 channel/hub 一起下线;这里
    # 传 "" 保持位置对齐,launch-vm.sh 位置参解析不动。
    cognito_arg = '""'
    # token (tenant_id EncryptionContext, ClawPool CMK). Empty ("") when the CMK
    # feature is off — launch-vm.sh keeps `openssl rand`ing its own token in-VM
    # ciphertext is already base64 text from core.kms_envelope.encrypt_with_tenant,
    # so no re-encoding — shell-quote it defensively even though base64url only
    # produces [A-Za-z0-9_-] (belt-and-suspenders behind the input validation
    # regex on tenant creation).
    gw_token_arg = _q(gateway_token_ct) if gateway_token_ct else '""'
    # device: deviceId + publicKey + roles + scopes, tokens:{} for 2026.2.26).
    # launch-vm.sh base64-decodes it and writes <stateDir>/devices/paired.json so
    # a remote WSS client (JDWS) preloaded with the matching device identity
    # connects to the in-VM gateway with NO manual approve. Empty ("") when no
    # device was minted (owner unknown / CMK off) → launch-vm.sh skips the write
    # is already base64 text from create_tenant, shell-quote defensively.
    device_paired_arg = _q(device_paired_b64) if device_paired_b64 else '""'
    # The live route is allocated by host-agent from the configured bitmap
    # range and committed to Redis after health succeeds. The historical
    # VM_PORT_BASE+vm_num DNAT family was never consumed by Edge and leaked one
    # rule per lifecycle retry. Keep host_port in the record for the live
    # bitmap route, but do not create a second rule here.
    cmd = (
        f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} "
        f"{tpl_arg} {restore_arg} {skills_arg} {vkey_arg} {csecret_arg} "
        f"{chat_ep_arg} {cognito_arg} {gw_token_arg} {device_paired_arg}"
    )
    # 三分:ok=True→翻 running;ok=False 且 rc==75→flock-skip(另一次同租户 launch 在跑,VM
    # 正被拉起)→保持 restoring 等重投收敛,【不回滚不释放 slot】;ok=False 且 rc!=75→真失败回滚。
    # `cmd` 只运行 launch-vm.sh;flock-skip 时脚本 exit 75,SSM ResponseCode 如实反映。
    # #565 G1 —— 墙钟预算由**调用点**传入,不在这里写死 300。
    # 分层理由:本模块是**机制**(怎么下发、怎么等),而"这个动作允许等多久"是**策略**,
    # 它取决于调用方处在哪个死线档下(restore 是 180s 档,执行段 120s)。写死 300 的后果不是
    # 变慢:restore 在 t=180 被死线执行者判 failed,而 launch-vm.sh 还能合法再跑 120s 并把 VM
    # 拉起来 —— 上层看到失败、底层其实成功,正是 #565 要消灭的那个状态不一致。
    # 默认值保留 300 是为了不改动其它(将来可能出现的)同步调用方的行为。
    if sync:
        return _ssm_run(instance_id, cmd, timeout=int(sync_timeout), want_rc=True)
    # Return the SSM CommandId (or None if submission failed — notably an SSM
    # SendCommand ThrottlingException under concurrent consumer fan-out, loop
    # 2026-07-01 real-machine bug). Callers on the create path check this: a
    # None means launch-vm never ran, so the tenant must be rolled back and the
    # create retried, not left stuck in `creating` with a leaked capacity slot.
    return _ssm_send(instance_id, cmd, timeout=300)


#: 生命周期脚本在 host 上的安装目录(init-host.sh 装到这里,自愈也只写这里)。
_HOST_SCRIPT_DIR = "/home/ubuntu"

#: launch-vm.sh 抢不到 per-tenant flock 时的退出码(launch-vm.sh:705 的 skip 专用哨兵)。
#: 语义是「另一次**同租户** launch 正在把 VM 拉起」——良性,不是失败。
RC_FLOCK_SKIP = 75

#: flock-skip 走 5xx 出口时响应 body 里的机器可读 code(#604)。
#:
#: 为什么需要一个 code 而不是只靠 HTTP 状态:503 在这条链上有多个成因(容量、下游不可达),
#: 而只有 flock-skip 这一种是「良性且短暂」的 —— consumer 据此把重投退避从队列默认的
#: 960s 缩到秒级(handler.py `_shorten_lifecycle_visibility_best_effort`),其余 5xx 的重投
#: 节奏一个字不改。判据放在 body 的 code 上,让"该缩谁"是显式声明而不是靠状态码猜。
LAUNCH_IN_PROGRESS_CODE = "LAUNCH_IN_PROGRESS"


def host_script_self_heal(scripts, tag, freshness=None):
    """生成一段"缺失/过期就从 S3 重新装载生命周期脚本"的 shell 前置片段。

    为什么需要:host 侧生命周期脚本(`stop-vm.sh` / `reset-vm.sh` / `rebuild-vm.sh` /
    `delete-vm.sh`)是 `init-host.sh` **开机时**从 `s3://$ASSETS_BUCKET/deployment/
    scripts/` 各自 `aws s3 cp` 装上去的。控制面先上线、host 是既有机器时,新加的脚本在
    那台机器上**根本不存在**(开机时它那版 init-host.sh 还没有那几行),旧版脚本也可能
    缺新语义;此时控制面无条件调用会 `exit 127` 或行为不一致。文档写"必须成对部署"
    不算保护——那是把正确性寄托在人工步骤上。`#469` 先在 delete 主路径解决了这件事,
    `#520 C2` 把同一兜底铺到 suspend / restore / reset / rebuild。

    契约:
    · 判据不只看文件存在。`freshness=(script, sentinel)` 会额外要求该脚本里能 grep 到
      `sentinel`——旧版存在但不认新语义,正是最难查的一档(如旧 `stop-vm.sh` 不认
      `OC_LIFECYCLE_LOCK_FD` → 15s 锁超时),必须一起换掉。
    · 触发 freshness 自愈后必须用同一判据复验;只有哨兵在位才算装载完成。若 S3 上
      那份脚本也过期、装完仍缺哨兵,必须 fail-loud 报部署不完整,不能放行后续命令。
    · **任何一步失败都 `exit 1`**,让整条 SSM 命令非零 → 调用方按各自的失败路径回滚或
      保留可重投状态。这条链上**不允许 `|| true`**:静默容错等于把不可逆操作变成
      best-effort(`test_ADV_route_cleanup_is_not_best_effort` 就是拦这个的)。
    · 装之前先 `bash -n` 语法检查,拒绝装一个半截文件。
    · 桶名与 region 由 host 侧自己从 `/etc/platform.env` 读(`init-host.sh` 写的那份,
      `launch-vm.sh` / `backup-data.sh` 也都这么取),**不从控制面拼进命令串** —— 否则
      Lambda 少注入一个环境变量就会拼出 `s3:///deployment/...` 这种坏 URI,而这段代码
      位于不可逆操作的主路径上,拼错的代价是所有该操作都失败。
    · 存在性判据走 `for f in <名字>` + `"$_HOST_SCRIPT_DIR/$f"`,**不写死
      `/home/ubuntu/<name>` 字面量**:字面量会让"按脚本路径定位真实调用点"的测试
      (`argv.index("/home/ubuntu/rebuild-vm.sh")`)命中判据而错位。
    · 正常情况(脚本已是新版)只做 N 次 `[ -x ]` 与最多一次 grep,开销可忽略,且**不增加
      SendCommand 次数**(同一条命令内的 shell 片段)。

    Args:
        scripts: 需要保证在位的脚本裸文件名序列(非空),如 `("stop-vm.sh",)`。
        tag: 日志前缀,如 `"oc:suspend"`;只允许可打印且不含单引号(要进单引号 echo)。
        freshness: 可选 `(script_name, sentinel)`,给"存在但过期"这一档加判据。

    Returns:
        可用 `&&` 串进命令链的 shell 片段(str)。
    """
    names = tuple(scripts)
    if not names:
        raise ValueError("host_script_self_heal: scripts 不能为空")
    for name in names:
        if "/" in name or not name or name != name.strip():
            raise ValueError(f"host_script_self_heal: 非法脚本名 {name!r}(只收裸文件名)")
    if "'" in tag or not tag:
        raise ValueError(f"host_script_self_heal: 非法 tag {tag!r}")

    name_list = " ".join(names)
    need = (
        "_oc_heal=; "
        f"for f in {name_list}; do "
        f'[ -x "{_HOST_SCRIPT_DIR}/$f" ] || _oc_heal=1; '
        "done; "
    )
    post_install_verify = ""
    if freshness is not None:
        probe_script, sentinel = freshness
        if probe_script not in names:
            raise ValueError(
                f"host_script_self_heal: freshness 脚本 {probe_script!r} 不在 scripts 内,"
                "过期判据命中后不会被重新装载"
            )
        if "/" in probe_script or not sentinel or any(
            c.isspace() for c in sentinel
        ):
            raise ValueError(
                f"host_script_self_heal: 非法 freshness {freshness!r}"
            )
        # 脚本名经一个 shell 变量转一手,拼出的完整路径不以字面量出现在片段里。
        # #523:此前这里写的是字面 `{_HOST_SCRIPT_DIR}/{probe_script}`,注释说"它是判据
        # 不是调用点"所以无妨 —— 那个前提只在【被探的脚本不是被调的脚本】时成立。给
        # rebuild-vm.sh 自己加过期判据时前提就破了:多处测试用
        # `argv.index("/home/ubuntu/rebuild-vm.sh")` 定位真实调用点并按位取参
        # (test_413_lifecycle_fence / test_416_rebuild_repin / test_rebuild_upgrade /
        # test_456 的 P0 注入面断言),字面量会让它们命中判据、错位取到 `||`。
        # 存在性判据早就为此走 `"$_HOST_SCRIPT_DIR/$f"` 的变量形式,这里对齐同一写法。
        # host 侧行为逐字不变:同一个 grep、同一个路径、同一个结果。
        need += (
            f"_oc_probe={probe_script}; "
            f'grep -q {sentinel} "{_HOST_SCRIPT_DIR}/$_oc_probe" 2>/dev/null '
            "|| _oc_heal=1; "
        )
        # install 成功只证明文件落盘,不能证明 S3 上那份已含控制面要求的新语义。
        # 复用同一个探针与同一个 grep 判据;S3 也过期时必须在放行日志之前 fail-loud。
        # 诊断必须【同时】写 stderr:控制面的失败分支只取 StandardErrorContent
        # (host 侧 log() 是裸 echo 到 stdout),只 echo 到 stdout 时 Lambda 日志里只剩
        # 一句 "SSM failed (rc=1)",运维拿不到原因 —— #503 就是这个坑造成的 P0 回归。
        # 2026-08-24 usw2 真机实测确认:复验失败时 StandardOutputContent 有完整诊断而
        # StandardErrorContent 为空,consumer 日志因此只有 rc=1。
        post_install_verify = (
            f'grep -q {sentinel} "{_HOST_SCRIPT_DIR}/$_oc_probe" 2>/dev/null || '
            f'{{ _oc_msg="[{tag}] FATAL 自愈后复验失败: $_oc_probe 缺失哨兵 {sentinel}; '
            'S3 上那份也缺该哨兵 = 部署不完整"; '
            'echo "$_oc_msg"; echo "$_oc_msg" >&2; exit 1; }; '
        )
    return (
        f"{need}"
        'if [ -n "$_oc_heal" ]; then '
        f"echo '[{tag}] host 脚本缺失/过期,从 S3 自愈装载'; "
        # 不用 `. /etc/platform.env || true` —— 这条链上【任何】`|| true` 都会被
        # test_ADV_route_cleanup_is_not_best_effort 拦下。改为先判可读再 source,
        # source 失败就让整段非零退出、走调用方的失败路径。
        "if [ -r /etc/platform.env ]; then set -a; . /etc/platform.env; set +a; fi; "
        '_B="${ASSETS_BUCKET:-}"; '
        f'[ -n "$_B" ] || {{ _oc_msg="[{tag}] FATAL 读不到 ASSETS_BUCKET"; '
        'echo "$_oc_msg"; echo "$_oc_msg" >&2; exit 1; }; '
        f"for f in {name_list}; do "
        'aws s3 cp "s3://$_B/deployment/scripts/$f" "/tmp/oc-heal-$f" '
        "--no-progress >/dev/null 2>&1 || "
        f'{{ _oc_msg="[{tag}] FATAL 拉取 $f 失败"; '
        'echo "$_oc_msg"; echo "$_oc_msg" >&2; exit 1; }; '
        'bash -n "/tmp/oc-heal-$f" || '
        f'{{ _oc_msg="[{tag}] FATAL $f 语法错误,拒绝安装"; '
        'echo "$_oc_msg"; echo "$_oc_msg" >&2; exit 1; }; '
        f'install -o root -g root -m 755 "/tmp/oc-heal-$f" "{_HOST_SCRIPT_DIR}/$f" || '
        f'{{ _oc_msg="[{tag}] FATAL 安装 $f 失败"; '
        'echo "$_oc_msg"; echo "$_oc_msg" >&2; exit 1; }; '
        "done; "
        f"{post_install_verify}"
        f"echo '[{tag}] 自愈装载完成'; "
        "fi"
    )


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


def _notify_result(cb, status, rc):
    """把终态 (status, rc) 交给 on_result 回调;回调抛异常只记日志,绝不影响 SSM 结果。"""
    if cb is None:
        return
    try:
        cb(status, rc)
    except Exception as e:  # noqa: BLE001 — 遥测不得让一次正常的 SSM 运行变成失败
        print(f"SSM on_result callback error: {e}")


def _notify_output(cb, result):
    """Expose terminal stdout/stderr without changing the bool return contract."""
    if cb is None:
        return
    try:
        cb(
            result.get("StandardOutputContent", ""),
            result.get("StandardErrorContent", ""),
        )
    except Exception as e:  # noqa: BLE001
        print(f"SSM on_output callback error: {e}")


_OUTPUT_PROPAGATION_RETRIES = 6
_OUTPUT_PROPAGATION_SLEEP_SECONDS = 1.0


def _await_command_output(cmd_id, instance_id, last_result):
    """Status=Success 后 stdout 仍为空时,有界重读 invocation 直到 stdout 非空。

    返回最后一次(或首个非空的)invocation 结果。刻意有界(最多
    _OUTPUT_PROPAGATION_RETRIES 次):命令【已经】终态成功,这里只等服务端把输出
    传播上来,绝不无限阻塞、绝不把成功翻成失败。填不满就返回最后一次结果(stdout
    仍空)—— 上游据此回落 unconfirmed,由 health-check reconciler 事后重取回执收敛。"""
    result = last_result
    for _ in range(_OUTPUT_PROPAGATION_RETRIES):
        time.sleep(_OUTPUT_PROPAGATION_SLEEP_SECONDS)
        try:
            result = ssm.get_command_invocation(
                CommandId=cmd_id, InstanceId=instance_id
            )
        except Exception as e:  # noqa: BLE001 — 重读失败就用上一次结果,不改成败判定
            print(f"SSM output re-read error {cmd_id}: {e}")
            break
        if (result.get("StandardOutputContent") or "").strip():
            break
    return result


def _ssm_run(
    instance_id,
    command,
    timeout=30,
    want_rc=False,
    on_command_id=None,
    on_result=None,
    on_output=None,
):
    """Execute command on host via SSM Run Command. Returns True on success.

    want_rc=True: return (ok: bool, rc: int|None) instead of bare bool, where rc is
    the host script's shell exit code (SSM ResponseCode). #422 restore needs this to
    tell a benign flock-skip (launch-vm.sh exits 75 when another launch of the SAME
    tenant already holds the per-tenant flock — see launch-vm.sh:395) apart from a
    real failure: on rc==75 the VM is being brought up by the concurrent/redelivered
    launch, so restore must NOT roll back. rc is None when we never got an invocation
    result (submit error / timeout / InvocationDoesNotExist throughout).

    on_command_id: optional callable invoked with the CommandId the instant SSM
    accepts the command — i.e. BEFORE the wait loop below can time out. Until now the
    id was obtained here and then thrown away on every non-Success path (the timeout
    branch only printed it), so a lost receipt left no handle to ask SSM about the
    execution afterwards even though SSM retains invocation records server-side.
    ADR-rebuild-idempotency-sync-contract §5.4a path 1 persists it
    (tenants.rebuild_ssm_command_id) so a later sweep can reconcile an `unconfirmed`
    rebuild instead of guessing. Passed as a callback rather than a new return value
    on purpose: the return contract stays byte-identical for every existing caller,
    and the id survives the timeout path. Callback errors are swallowed — telemetry
    must never turn a healthy SSM run into a failure.

    on_result: optional callable invoked with (status: str, rc: int|None) the moment a
    terminal invocation result arrives. Exists for the same reason as on_command_id —
    callers that need the exit code should not have to flip want_rc, because that
    changes the return type from bool to tuple and every existing caller plus ~70 test
    stubs would have to unpack it (rebuild hit exactly this wall). The rc matters
    because launch-vm.sh exits 75 to say "another launch of this same tenant holds the
    per-tenant flock, I skipped — retry later", which is a BENIGN outcome, not a
    failure: reporting it as a confirmed failure tells the customer a retry is safe,
    and a repeated rebuild drops the per-VM overlay again. Callback errors are
    swallowed for the same reason as above.

    on_output: optional callable invoked with (stdout, stderr) for a terminal
    invocation. #413 uses it to validate the host's op-specific rebuild evidence;
    the normal bool return cannot carry that proof."""
    try:
        # SSM runs as root; set HOME so ~ resolves to /home/ubuntu
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        # #565 G1(Codex 独立复审第 2 轮)—— **计时必须从 `send_command` 之前开始。**
        # 原来起在它之后,于是 `timeout` 只界住轮询、**不界住 send 本身** —— 而 #573 给 ssm
        # client 加了 adaptive 重试之后,一次被节流的 `SendCommand` 最坏也要 ~71s 退避。
        # 那 71s 白白落在预算之外,于是「执行段」不是真实上界,调用方按它排的死线就会破。
        # `time.monotonic()` 而非 `time.time()`:后者会被 NTP 校正拖动。
        _deadline = time.monotonic() + timeout
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
        if on_command_id is not None:
            try:
                on_command_id(cmd_id)
            except Exception as e:  # noqa: BLE001 — never fail the run for telemetry
                print(f"SSM on_command_id callback error: {e}")
        # #565 G1 —— `timeout` 必须是**墙钟预算**,不是轮数。
        #
        # 原来是 `for _ in range(timeout // 2)`,即轮数上限;它等于墙钟上界只在「每轮恰好
        # 2s」这个隐含假设下成立。**#573 打破了那个假设**:它给 `core/clients.ssm` 加了
        # `retries={"max_attempts": 8, "mode": "adaptive"}`(防 SendCommand 节流毒 DLQ,
        # 那件事本身是对的)。于是单次 `get_command_invocation` 被节流时最坏要等 7 次重试的
        # 指数退避 —— botocore `ExponentialBackoff` 的 `_MAX_BACKOFF=20`、基数 2,实测退避
        # 总和上界 `1+2+4+8+16+20+20 = 71s`,adaptive 的客户端 token-bucket 限速还在这之上。
        # 一轮就可能 73s,`timeout=300` 的 150 轮理论上界约 11000s。
        #
        # 这条对 #565 G1 是前提性的:G1 要求「执行段必须 ≥ 该操作最坏 SSM 耗时」,而**没有
        # 真实墙钟上界就没有可算的执行段**。对调用方则是正确性问题 —— 谁按 `timeout` 当上界
        # 排预算,都会在它超出时留下「上层已判死、host 侧还在跑」的孤儿(#562 §2.2 那条)。
        #
        # deadline 检查放在**发下一次 API 调用之前**:超预算就不再发新请求。净上界 =
        # `timeout` + 最后那次调用自身耗时(最坏 ~71s 退避)。
        # `time.monotonic()` 而非 `time.time()`:后者会被 NTP 校正拖动。
        time.sleep(3)  # Wait for invocation to register
        while time.monotonic() < _deadline:
            try:
                result = ssm.get_command_invocation(
                    CommandId=cmd_id,
                    InstanceId=instance_id,
                )
                status = result["Status"]
                if status == "Success":
                    # Success(服务端输出传播延迟)。用 on_output 解析 host 带内证据的调用方
                    # 已吐出完整 identity+inode 的 SUCCEEDED JSON,但 Success 当刻第一次
                    # get-command-invocation 的 StandardOutputContent 仍为空 → _rb_host_result
                    # =None → 假 unconfirmed + lease 泄漏。仅当【调用方消费 output 且 stdout
                    # 仍为空】时有界重读,补齐传播缺口;其余调用方路径逐字节不变。填不满也不阻塞、
                    # 不把成功翻成失败 —— 回落 unconfirmed 由 health-check reconciler 兜底收敛。
                    if on_output is not None and not (
                        result.get("StandardOutputContent") or ""
                    ).strip():
                        result = _await_command_output(cmd_id, instance_id, result)
                    _notify_output(on_output, result)
                    _notify_result(on_result, status, result.get("ResponseCode", 0))
                    return (True, result.get("ResponseCode", 0)) if want_rc else True
                if status in ("Failed", "TimedOut", "Cancelled"):
                    rc = result.get("ResponseCode")
                    print(
                        f"SSM failed: {status} (rc={rc}) - "
                        f"{result.get('StandardErrorContent', '')}"
                    )
                    _notify_output(on_output, result)
                    _notify_result(on_result, status, rc)
                    return (False, rc) if want_rc else False
            except ssm.exceptions.InvocationDoesNotExist:
                pass
            time.sleep(2)
        print(f"SSM timeout waiting for command {cmd_id}")
        return (False, None) if want_rc else False
    except Exception as e:
        print(f"SSM error: {e}")
        return (False, None) if want_rc else False
