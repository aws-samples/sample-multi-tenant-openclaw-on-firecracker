# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#394 step2-3 — 不可变版本目录 + slots.json 槽位指针(ADR §4.1 / §4.2)。

为什么存在:今天一台 host 只有一套扁平 live 盘(`/data/firecracker-assets/openclaw-*.ext4`,
`launch-vm.sh:631`),pull 直接 mv 覆盖它。这做不到 live/canary 并存,也做不到"验证的版本
就是提升的版本"——因为没有任何地方记录"这套盘是哪个 snapshot"。

本模块给出两件东西的【纯函数】定义(不做 IO,便于单测):
  1. 版本目录布局:`versions/<snapshot_time>/openclaw-<kind>.ext4` + `manifest.json`,
     装完即不再修改(不可变)。
  2. `slots.json` 指针:`{generation, live, canary, previous_live}`,promote/rollback/
     cleanup 只改这一个小文件。

提交协议(ADR §4.1,shell 侧实现见 `slots_write_lines`):同文件系统写 `.tmp` → 校验
JSON → `fsync(file)` → 原子 `rename` → `fsync(parent dir)`。只有一个提交点,断电后
"旧 slots.json 或新 slots.json 必有且仅有一份有效"(ADR §11.2)。
"""

import json
import shlex

# 与 host_service._LIVE 同一目录(launch-vm 读的地方);版本目录与 slots.json 都在其下。
LIVE_ROOT = "/data/firecracker-assets"

VERSIONS_DIR = f"{LIVE_ROOT}/versions"

SLOTS_FILE = f"{LIVE_ROOT}/slots.json"

# 合法槽位名。canary 之外不开放任意槽位:多一个槽就多一条要对账的路径(ADR §10 拒绝
# "任意 image delete API" 同源理由 —— 面越窄越可证)。
SLOT_LIVE = "live"
SLOT_CANARY = "canary"
VALID_SLOTS = (SLOT_LIVE, SLOT_CANARY)

# 镜像三盘的 kind(与 manifest 字段、既有扁平名一致)。
DISK_KINDS = ("rootfs", "data-template", "immutable")


def is_valid_slot(slot):
    return slot in VALID_SLOTS


def version_dir(snapshot_time):
    """某快照的不可变版本目录。snapshot_time 已由 API 层正则校验过格式。"""
    return f"{VERSIONS_DIR}/{snapshot_time}"


def disk_path(snapshot_time, kind):
    """版本目录内某盘的绝对路径(扁平 kind 名,与既有 live 命名一致 → launch-vm 好拼)。"""
    return f"{version_dir(snapshot_time)}/openclaw-{kind}.ext4"


def manifest_path(snapshot_time):
    return f"{version_dir(snapshot_time)}/manifest.json"


def complete_marker_path(snapshot_time):
    """#394 —— 版本目录的"完整"标记。三盘 + manifest 全部 commit 成功后【最后】原子写这个文件;
    它是"这套版本目录已装齐、可信"的唯一信号。

    为什么需要:版本目录不是原子创建的(三个盘逐个 mv 进去),下载/解压到一半盘满/掉电会留下
    只有部分盘的【半装目录】。若 pull 快路径拿"目录存在"判"已装好",就会把残缺目录翻成 live →
    VM 起不来(no-data-loss)。故判据是【标记存在】而非【目录存在】:半装目录没有标记 → 快路径
    不认它 → 走正常重下自愈。语义同 launch-vm 的 live done-marker(提交点是最后一个小文件)。"""
    return f"{version_dir(snapshot_time)}/.complete"


def version_complete_check_lines(snapshot_time, on_complete_var="VER_COMPLETE"):
    """生成"该版本目录是否已完整装好"的 shell 判据行(设 $<on_complete_var>=1/0)。

    判据:版本目录存在 + .complete 标记存在 + manifest.json 存在 + 标记里记的三盘都在且非空。
    标记文件内容 = 换行分隔的盘文件名(写标记时列出),这里逐个 `[ -s ]` 复核,防"标记写了但盘
    被后续误删/截断"。任一不满足 → 判不完整(0),调用方走正常重下(fail-safe:宁可重下不可
    翻半盘)。纯判据,不改任何东西。"""
    q = shlex.quote
    vd = q(version_dir(snapshot_time))
    marker = q(complete_marker_path(snapshot_time))
    man = q(manifest_path(snapshot_time))
    return [
        f"{on_complete_var}=0",
        f'if [ -d {vd} ] && [ -f {marker} ] && [ -f {man} ]; then',
        f"  {on_complete_var}=1",
        # 复核标记里列出的每个盘仍在且非空(标记每行一个盘的绝对路径)。
        f'  while IFS= read -r _d; do [ -n "$_d" ] || continue; [ -s "$_d" ] || {on_complete_var}=0; done < {marker}',
        "fi",
    ]


def write_complete_marker_lines(snapshot_time, disk_paths):
    """生成"原子写 .complete 标记"的 shell 行(内容 = 三盘绝对路径,每行一个)。

    在版本目录三盘 + manifest 全部 commit 成功【之后】、翻 slots 指针【之前】调用。tmp+rename 原子写:
    标记出现 = 全套已就位;标记不出现(写之前崩)= 快路径判不完整、重下自愈。"""
    q = shlex.quote
    marker = complete_marker_path(snapshot_time)
    body = "\n".join(disk_paths)
    return [
        f"printf '%s\\n' {q(body)} > {q(marker + '.tmp')}",
        f"mv {q(marker + '.tmp')} {q(marker)} "
        f'|| {{ _perr "COMPLETE_MARKER_FAILED could not write {marker}"; exit 1; }}',
    ]


def empty_slots():
    """初始 slots(还没任何版本目录时的形态)。generation 从 0 起。"""
    return {"generation": 0, "live": None, "canary": None, "previous_live": None}


def normalize(raw):
    """把读到的 slots.json 收敛成完整结构(缺字段给默认值)。

    容忍:文件不存在 / 空 / 少字段 —— 这些都是"尚未导入版本目录"的老 host 的正常形态,
    不能因此拒绝启动(向后兼容,ADR §12 step2 "保留兼容路径")。
    不容忍:JSON 解析失败或不是对象 —— 那是损坏,调用方应 fail-loud 而不是当空处理
    (静默当空会让 live 指针"凭空消失",进而让 launch-vm 回落到旧扁平盘 = 起错版本)。
    """
    if raw is None or raw == "":
        return empty_slots()
    if isinstance(raw, (str, bytes)):
        parsed = json.loads(raw)  # 损坏则抛 JSONDecodeError,由调用方 fail-loud
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        raise ValueError("slots.json must be a JSON object")
    out = empty_slots()
    out["generation"] = int(parsed.get("generation") or 0)
    for key in ("live", "canary", "previous_live"):
        out[key] = parsed.get(key) or None
    return out


def apply_pull(slots, slot, snapshot_time):
    """pull 装完某槽后的新 slots(纯函数,不写盘)。

    · slot=canary:只填 canary,live/previous_live 完全不动 —— 这正是"canary 安装不影响
      同 host 存量 live 租户"的根据(ADR §11.1)。
    · slot=live:直接把 live 指到新版,原 live 落到 previous_live(留作 rollback 锚点)。
      仅当新旧不同才移动 previous_live:重复装同版是幂等的,不该把 previous_live 冲成自己
      (否则 rollback 目标变成当前版本 = 回滚成空操作)。
    generation 每次成功变更 +1(promote/rollback/cleanup 的 expected-generation CAS 靠它)。
    """
    if not is_valid_slot(slot):
        raise ValueError(f"invalid slot: {slot!r}")
    out = dict(slots)
    if slot == SLOT_CANARY:
        out["canary"] = snapshot_time
    else:
        if slots.get("live") and slots["live"] != snapshot_time:
            out["previous_live"] = slots["live"]
        out["live"] = snapshot_time
    out["generation"] = int(slots.get("generation") or 0) + 1
    return out


def apply_promote(slots, expected_snapshot_time, expected_generation):
    """canary 提升为 live(ADR §4.5)。返回 (new_slots, already, err)。

    CAS 语义:真正防"验证了 A、并发 pull 换成 B、结果提升了 B"的,是 **canary 的
    snapshot_time 相等** —— 若 canary 被换,snapshot_time 一定不同,这里就拒。
    generation 只作【软性】提示,【不】单独因它拒绝 promote:generation 会被其它操作
    (镜像回写 / self-heal / 上一轮 pull)递增,调用方手里的 generation 常常滞后于 host,
    而它们指的仍是【同一个】canary snapshot —— 拿滞后的 generation 硬拒 = 假冲突(真机实测:
    canary 都是 16:26:10Z,只因 gen 4≠2 就 CANARY_CHANGED,promote 永远做不成)。
    故:snapshot 相等即视为"验证的就是这个版本",放行;snapshot 不等才 CANARY_CHANGED。
    already:live 已是 expected 版本 → 幂等成功(响应丢失重试的正常路径,ADR §4.9 末段)。
    """
    cur_gen = int(slots.get("generation") or 0)
    if slots.get("live") == expected_snapshot_time:
        # live 已是目标版本 → 提升是 no-op。【无论 canary 是否也等于它】都幂等返回,绝不往下走:
        # 否则 canary==live==expected 时会把 previous_live 冲成 =live 的退化态(live==previous_live,
        # UI 显示重复的 prev,rollback/reclaim 语义也错)。#394 —— pull 侧 CANARY_EQUALS_LIVE 已挡
        # 住新建这种态,这里是 promote 的对称防御(旧数据 / 并发路径仍可能到达)。
        return slots, True, None      # 已提升过 / canary 与 live 同版 → 幂等,不动 previous_live
    if slots.get("canary") != expected_snapshot_time:
        return None, False, "CANARY_CHANGED"
    # 注:不再因 expected_generation != cur_gen 拒绝(见 docstring:snapshot 相等已足够证明
    # "验证的就是这个版本";generation 滞后是镜像/self-heal 递增的正常现象,不是并发换版)。
    out = dict(slots)
    out["previous_live"] = slots.get("live")
    out["live"] = expected_snapshot_time
    out["canary"] = None
    out["generation"] = cur_gen + 1
    return out, False, None


# #394 —— apply_rollback 已移除:回滚不再是 live↔previous_live 的 swap,而是"pull 老版到 live"
# (pull-image 快路径:本地版本目录已 .complete → 秒级翻 slots.live 指针,零下载)。这与 Lambda
# alias / K8s revision 的"选定一个保留的版本重指指针"模型一致。previous_live 槽仍保留作纯展示。


# #394 —— apply_cleanup_canary 已移除:cleanup-canary(DELETE image-slots/canary)接口已删,
# 精简 API。放弃未提升的 canary 靠下次 `pull-image?slot=canary` 覆盖该槽 / promote 成功清空;
# 磁盘回收由 reclaim-images 承担。referenced_versions 仍把 canary 纳入保护名单(见下)。


def referenced_versions(slots):
    """slots 仍引用的版本集合 —— GC 与 cleanup 的保护名单(ADR §4.7)。

    live / previous_live / canary 三者都不可回收;租户固定引用的版本由控制面另行叠加
    (那部分要查 tenants 表,不在纯函数里做)。
    """
    return {v for v in (slots.get("live"), slots.get("canary"),
                        slots.get("previous_live")) if v}


def reclaim_versions_lines(keep, versions_dir=VERSIONS_DIR):
    """#394 —— 生成"删掉 versions/ 下不在 keep 集合里的版本目录"的 shell 行(手动 prune)。

    保护面由【控制面】算好后作为**显式白名单**传进来(keep 已含 live/canary/previous_live +
    所有非 deleted 租户仍固定引用的版本 —— 那需要查 tenants 表,纯函数里做不了),host 侧
    只做减法:枚举 versions/ 的每个目录名,不在 keep 里才 rm -rf。为何白名单而非黑名单:
    launch-vm 把 versions/<snap>/rootfs 当 COW 只读底盘(launch-vm.sh:677),删掉在用版本 =
    下次起 VM 丢底盘(no-data-loss)。宁可漏删(下轮再收)不可误删。

    安全约束:
      · versions/ 不存在 → 空输出(老扁平 host 没有版本目录,不是错误)。
      · ls 失败(权限/IO)→ fail-loud 退出,【绝不】在读不到全量清单时删任何东西。
      · 目录名经 keep 精确匹配;keep 里的 snapshot_time 已由 API 层正则校验格式。
      · 每个删除对象都 realpath 限定在 versions/ 下(防目录名里塞 `..`/软链逃逸)。
    返回:删除了哪些版本以 `__RECLAIMED__<name>` 行打印,供控制面解析回报。
    """
    q = shlex.quote
    vd = q(versions_dir)
    # keep 集合注入成 shell case 分支的精确字面量(每个已正则校验,不含 shell 元字符)。
    # 用 case 而非数组遍历:POSIX sh 稳、O(1) 命中、字面量精确相等(不做 glob)。
    keep_cases = "|".join(q(k) for k in sorted(keep)) or "__NONE_KEEP__"
    return [
        f'[ -d {vd} ] || {{ echo "__RECLAIM_DONE__no versions dir"; exit 0; }}',
        # 先把 versions/ 本身解析成规范路径(它可能经过软链,如 /tmp→/private/tmp),逃逸判据
        # 拿【已解析的 base】比子项已解析路径,不能拿原始字面量比(否则 base 含软链时全被误拒)。
        f'_base="$(realpath {vd} 2>/dev/null)" || {{ _perr "RECLAIM_FAILED cannot resolve versions dir"; exit 1; }}',
        # ls -1 失败(而非空)必须 fail-loud:读不到全量清单时删除 = 可能把在用版本当孤儿删。
        f'_names="$(ls -1 {vd} 2>/dev/null)" || {{ _perr "RECLAIM_FAILED cannot list versions dir"; exit 1; }}',
        'while IFS= read -r _n; do',
        '  [ -n "$_n" ] || continue',
        f'  case "$_n" in {keep_cases}) continue ;; esac',
        f'  _p="{versions_dir}/$_n"',
        # 逃逸防护:子项解析后必须仍落在【已解析的 base】下(目录名含 ..、或软链指向别处 → 跳过)。
        '  _rp="$(realpath "$_p" 2>/dev/null || true)"; case "$_rp" in "$_base"/*) : ;; *) _perr "RECLAIM_SKIP unsafe path $_n"; continue ;; esac',
        '  rm -rf "$_p" && echo "__RECLAIMED__$_n" || _perr "RECLAIM_SKIP rm failed $_n"',
        'done <<EOF_RECLAIM',
        '$_names',
        'EOF_RECLAIM',
        'echo "__RECLAIM_DONE__ok"',
    ]


def slots_write_lines(new_slots, slots_file=SLOTS_FILE):
    """生成"原子写 slots.json"的 shell 行(ADR §4.1 提交协议)。

    tmp 与目标【同目录】(同文件系统)→ rename 才是原子的;跨文件系统 rename 会退化成
    copy+unlink,断电时可能留半个文件。写完 fsync 文件再 fsync 父目录:只 fsync 文件时,
    目录项可能还没落盘,断电后 rename 丢失(经典 ext4 陷阱)。
    python3 兜 JSON 校验:host 上有 python3(init-host 依赖它解析 manifest,
    init-host.sh:484),写坏的 JSON 宁可当场失败也不要 rename 上去。
    """
    payload = json.dumps(new_slots, sort_keys=True, separators=(",", ":"))
    q = shlex.quote
    f = q(slots_file)
    tmp = q(slots_file + ".tmp")
    return [
        f"printf '%s' {q(payload)} > {tmp}",
        f'python3 -c "import json,sys; json.load(open(sys.argv[1]))" {tmp} '
        f'|| {{ _perr "SLOTS_WRITE_FAILED staged slots.json is not valid JSON"; exit 1; }}',
        f'python3 -c "import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)" {tmp} '
        f'|| {{ _perr "SLOTS_WRITE_FAILED fsync tmp failed"; exit 1; }}',
        f"mv {tmp} {f} "
        f'|| {{ _perr "SLOTS_WRITE_FAILED atomic rename failed"; exit 1; }}',
        f'python3 -c "import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)" '
        f"{q(LIVE_ROOT)} || true",  # 父目录 fsync 失败不回滚(已 rename),尽力而为
    ]


def slots_fence_guard_lines(hosts_table, region, instance_id, op_id, fence_epoch):
    """#394 P1-1 —— slot 提交【前】的 host 侧 fence 门(挡"超时的旧 SSM 命令晚到覆盖新操作")。

    问题:promote/cleanup/reclaim 的 SSM 命令若 60s 未确认,Lambda 返回 503 并在 finally 释放
    lease → 别的操作抢到新 lease(fence_epoch +1)。但那条【旧 SSM 命令仍在 host 上继续跑】,
    Lambda 侧的 fence_valid() 拦不住已下发的 host 命令。故 fence 必须落在 host 脚本里,在原子
    rename slots.json 之前:强一致读 DDB,确认 active_image_operation_id==本 op 且 image_fence_epoch
    ==本 epoch,任一不符即 exit 1(绝不 rename)。

    另加 host 级 flock(与 pull 同一把 pull.lock):序列化 host 侧的所有镜像写,连带堵住
    "live pull 脚本与 slot-op 脚本同时写 slots.json"的 host 侧窗口(P1-2 的 host 侧那一半)。
    flock fd 保持打开到进程退出,rename 全程在锁内。
    返回的 shell 行应【拼在 slots_write_lines 之前】。"""
    q = shlex.quote
    key = q(json.dumps({"instance_id": {"S": instance_id}}))
    return [
        # host 级固定锁(与 pull 脚本 exec 9>pull.lock 同一把),阻塞等锁,序列化 host 侧镜像写。
        f"exec 9>{q(LIVE_ROOT)}/pull.lock",
        'flock -w 60 9 || { _perr "SLOT_LOCK_HELD another image op holds the host lock >60s"; exit 1; }',
        # 强一致读 lease owner + fence_epoch + expiry(与 pull fence 同款,读失败重试一次)。
        f'FENCE=$(aws dynamodb get-item --table-name {q(hosts_table)} --region {q(region)} '
        f'--key {key} --consistent-read '
        f'--query "[Item.active_image_operation_id.S, Item.image_fence_epoch.N, Item.image_lease_until.N]" --output text 2>/dev/null) || FENCE=""',
        f'if [ -z "$FENCE" ]; then FENCE=$(aws dynamodb get-item --table-name {q(hosts_table)} '
        f'--region {q(region)} --key {key} --consistent-read '
        f'--query "[Item.active_image_operation_id.S, Item.image_fence_epoch.N, Item.image_lease_until.N]" --output text 2>/dev/null) || FENCE=""; fi',
        'if [ -z "$FENCE" ]; then _perr "SLOT_FENCE_READ_FAILED cannot read lease from DDB"; exit 1; fi',
        '_OWNER=$(printf "%s" "$FENCE" | cut -f1); _EPOCH=$(printf "%s" "$FENCE" | cut -f2); _UNTIL=$(printf "%s" "$FENCE" | cut -f3)',
        f'if [ "$_OWNER" != {q(op_id)} ]; then _perr "SLOT_FENCED lease owner=$_OWNER not {op_id}; superseded, abort"; exit 1; fi',
        f'if [ "$_EPOCH" != {q(str(int(fence_epoch)))} ]; then _perr "SLOT_FENCED fence_epoch=$_EPOCH not {int(fence_epoch)}; superseded, abort"; exit 1; fi',
        'if [ -z "$_UNTIL" ] || [ "$_UNTIL" -le "$(date +%s)" ]; then _perr "SLOT_FENCED lease expired; abort"; exit 1; fi',
    ]


def slots_mirror_writeback_lines(hosts_table, region, instance_id, slots_env="SLOTS_NEW"):
    """#394 —— host 侧把最终 slots.json 原样回写 DDB hosts.image_slots(消除镜像漂移)。

    之前控制面 image_slots 只由 Lambda 增量 patch canary,live/generation/previous_live
    会与 host 真值对不上 → promote 误报 CANARY_CHANGED、UI live 显示 null。改由【host 自己】
    在写完 slots.json 后,把那份权威 JSON 转成 DDB Map 格式写回 hosts.image_slots —— 单一
    真相源(host),不再靠 Lambda 猜。host 角色有 hosts 表 UpdateItem 权(心跳在用)。

    读 shell 变量 `$SLOTS_NEW`(_slots_commit_lines 里 python3 打印的最终 JSON),用 python3
    转成 `{"M":{...}}` 再交 aws dynamodb update-item。best-effort(|| true):镜像回写失败不
    该让已成功的 slots 提交判失败;下一次镜像操作会再纠。各值 shell-quote 防注入。
    """
    q = shlex.quote
    # python3:把 $SLOTS_NEW(纯 JSON)转成 DDB update-item 需要的 ExpressionAttributeValues。
    # live/canary/previous_live 为 null → {"NULL":true};generation → {"N":"<int>"}。
    py = (
        "import json,os,sys,time\n"
        f"s=json.loads(os.environ[{slots_env!r}])\n"
        "def dv(v):\n"
        "    return {'NULL':True} if v is None else {'S':str(v)}\n"
        "m={'live':dv(s.get('live')),'canary':dv(s.get('canary')),"
        "'previous_live':dv(s.get('previous_live')),"
        "'generation':{'N':str(int(s.get('generation') or 0))}}\n"
        "print(json.dumps({':m':{'M':m},':ts':{'N':str(int(time.time()))}}))\n"
    )
    key_json = '{"instance_id":{"S":"' + instance_id + '"}}'
    # $SLOTS_NEW 已是当前 shell 的环境变量(export 到子进程 python3);heredoc 里 os.environ 读它。
    return [
        f"export {slots_env}",
        f"MIRROR_VALS=$(python3 - <<'MIRROREOF'\n{py}MIRROREOF\n) || MIRROR_VALS=''",
        # 有值才写(转换失败则跳过,不写坏镜像)。
        'if [ -n "$MIRROR_VALS" ]; then '
        f"aws dynamodb update-item --table-name {q(hosts_table)} --region {q(region)} "
        f"--key {q(key_json)} "
        f'--update-expression "SET image_slots = :m, image_slots_synced_at_epoch = :ts" '
        f'--expression-attribute-values "$MIRROR_VALS" >/dev/null 2>&1 '
        f'|| _p "WARN: slots mirror writeback failed (non-fatal; next op reconciles)"; fi',
    ]
