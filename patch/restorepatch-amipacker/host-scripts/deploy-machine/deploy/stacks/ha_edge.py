# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from urllib.parse import urlsplit
from aws_cdk import (
    aws_lambda as _lambda,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_autoscaling as autoscaling,
    aws_elasticloadbalancingv2 as elbv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_bedrock_agentcore_alpha as agentcore,
    aws_bedrockagentcore as agentcore_l1,
    aws_ssm as ssm,
    aws_elasticache as elasticache,
    aws_s3_deployment as s3deploy,
    custom_resources as cr,
    Duration,
    Fn,
    Token,
)
from pathlib import Path

from stacks._helpers import host_golden_ami_parameter_name
from stacks._helpers import track_default_lt_version
from stacks.edge_bundle import BUNDLE_OBJECT_NAME as EDGE_BUNDLE_OBJECT_NAME
from stacks.edge_bundle import build_edge_bundle


def _valid_s3_bucket_name(bucket):
    return bool(
        re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        and ".." not in bucket
        and ".-" not in bucket
        and "-." not in bucket
        and not re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", bucket)
    )


def _parse_user_hook(cfg, role):
    """Validate one optional root hook without accepting shell-shaped input."""
    user_hooks = cfg.get("user_hooks")
    if user_hooks is None:
        user_hooks = {}
    if not isinstance(user_hooks, Mapping):
        raise ValueError("user_hooks must be a mapping")
    raw = user_hooks.get(role)
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"user_hooks.{role} must be a mapping")
    uri = str(raw.get("s3_uri") or "").strip()
    if not uri:
        return None

    parsed = urlsplit(uri)
    key = parsed.path[1:] if parsed.path.startswith("/") else ""
    if (
        parsed.scheme != "s3"
        or not _valid_s3_bucket_name(parsed.netloc)
        or not key
        or parsed.path.startswith("//")
        or key.endswith("/")
        or parsed.query
        or parsed.fragment
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in uri)
        or "*" in key
        or "?" in key
    ):
        raise ValueError(
            f"user_hooks.{role}.s3_uri must be an exact private "
            "s3://bucket/object URI (no prefix, wildcard, query, or fragment)"
        )

    sha256 = str(raw.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(
            f"user_hooks.{role}.sha256 must be the object's full 64-char SHA256"
        )

    timeout = raw.get("timeout_seconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError(f"user_hooks.{role}.timeout_seconds must be an integer")
    if not 1 <= timeout <= 3600:
        raise ValueError(
            f"user_hooks.{role}.timeout_seconds must be between 1 and 3600"
        )

    failure_policy = str(raw.get("failure_policy") or "fail").strip().lower()
    if failure_policy not in {"fail", "warn"}:
        raise ValueError(f"user_hooks.{role}.failure_policy must be fail or warn")

    return {
        "uri": uri,
        "bucket": parsed.netloc,
        "key": key,
        "sha256": sha256,
        "timeout": timeout,
        "failure_policy": failure_policy,
    }


def _render_user_hook(
    role,
    hook,
    region_expr,
    fail_cleanup=":",
    install_dir="/var/lib/openclaw/user-hooks",
):
    """Return a bounded download/verify/execute block for Host or Edge."""
    if hook is None:
        return ""

    uri = shlex.quote(hook["uri"])
    sha256 = shlex.quote(hook["sha256"])
    hook_dir = shlex.quote(install_dir)
    timeout = int(hook["timeout"])
    failure = hook["failure_policy"]
    fail_lines = (
        [
            f'echo "[oc:user-hook] FATAL role={role} rc=${{_oc_hook_rc}}" >&2',
            fail_cleanup,
            'exit "${_oc_hook_rc}"',
        ]
        if failure == "fail"
        else [f'echo "[oc:user-hook] WARN role={role} rc=${{_oc_hook_rc}}" >&2']
    )
    lines = [
        f'echo "[oc:user-hook] START role={role}"',
        f"_oc_hook_uri={uri}",
        f"_oc_hook_sha={sha256}",
        f"_oc_hook_region=\"{region_expr}\"",
        f"_oc_hook_dir={hook_dir}",
        f'_oc_hook_path="${{_oc_hook_dir}}/{role}.sh"',
        '_oc_hook_next="${_oc_hook_path}.new"',
        '_oc_hook_tmp=$(mktemp "/tmp/openclaw-user-hook.XXXXXX")',
        "_oc_hook_rc=0",
        'if aws s3 cp "${_oc_hook_uri}" "${_oc_hook_tmp}" '
        '--region "${_oc_hook_region}" --no-progress; then',
        "  if printf '%s  %s\\n' \"${_oc_hook_sha}\" \"${_oc_hook_tmp}\" "
        "| sha256sum -c - >/dev/null; then",
        '    if install -d -m 0755 "${_oc_hook_dir}" '
        '&& install -m 0700 "${_oc_hook_tmp}" "${_oc_hook_next}" '
        '&& mv -f "${_oc_hook_next}" "${_oc_hook_path}"; then',
        f'      if OC_REGION="${{_oc_hook_region}}" OC_NODE_ROLE={role} '
        "timeout --signal=TERM --kill-after=10s "
        f'{timeout}s bash "${{_oc_hook_path}}"; then',
        f'        echo "[oc:user-hook] PASS role={role}"',
        "      else",
        "        _oc_hook_rc=$?",
        "      fi",
        "    else",
        "      _oc_hook_rc=$?",
        "    fi",
        "  else",
        "    _oc_hook_rc=$?",
        "  fi",
        "else",
        "  _oc_hook_rc=$?",
        "fi",
        'rm -f "${_oc_hook_tmp}" "${_oc_hook_next}"',
        'if [ "${_oc_hook_rc}" -ne 0 ]; then',
        *[f"  {line}" for line in fail_lines],
        "fi",
        "unset _oc_hook_uri _oc_hook_sha _oc_hook_region _oc_hook_dir "
        "_oc_hook_path _oc_hook_next _oc_hook_tmp _oc_hook_rc",
    ]
    return "\n".join(lines)


def build_ha_edge(self, ctx):
    """Build ha_edge resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    ac_cfg = getattr(ctx, "ac_cfg", None)
    ac_enabled = getattr(ctx, "ac_enabled", None)
    ac_gateway = getattr(ctx, "ac_gateway", None)
    amp_remote_write_url = getattr(ctx, "amp_remote_write_url", None)
    api_fn = getattr(ctx, "api_fn", None)
    assets_bucket = getattr(ctx, "assets_bucket", None)
    gateway_url = getattr(ctx, "gateway_url", None)
    health_fn = getattr(ctx, "health_fn", None)
    host_role = getattr(ctx, "host_role", None)
    sec_cfg = getattr(ctx, "sec_cfg", None)
    vpc = getattr(ctx, "vpc", None)

    # `_az_count` controls how many AZs the ASG and ALB span. Default is
    # single-AZ to minimize cross-AZ data-transfer charges; opt in via
    # config.yml `multi_az.enabled: true`.
    _multi_az = CFG.get("multi_az", {}) or {}
    _az_count = (
        int(_multi_az.get("az_count", 2)) if _multi_az.get("enabled", False) else 1
    )

    sg = ec2.SecurityGroup(
        self,
        "HostSG",
        vpc=vpc,
        security_group_name="openclaw-host-sg",
        allow_all_outbound=True,
    )
    # 开发期 SSH:config host.ssh_ingress_sg 配了堡垒机 SG id,就放它入站 22
    # (绝不对 0.0.0.0/0 开 22,守红线;只允许指定堡垒机 SG)。生产留空=不开 22。
    _ssh_sg = (CFG.get("host", {}) or {}).get("ssh_ingress_sg") or None
    if _ssh_sg:
        sg.add_ingress_rule(
            ec2.Peer.security_group_id(_ssh_sg),
            ec2.Port.tcp(22),
            "SSH from bastion SG only (dev; never 0.0.0.0/0)",
        )

    # Compute allocatable resources from instance type. Fallback to the
    _arch_default = (
        "m8g.xlarge"
        if (CFG.get("host", {}) or {}).get("arch") == "arm64"
        else "m8i.xlarge"
    )
    _itype = (CFG.get("host", {}) or {}).get("instance_type") or _arch_default
    # vCPU count by size token. Handles virtual sizes ("2xlarge") and
    # bare-metal sizes. Graviton metal uses a "metal-24xl"/"metal-48xl"
    # suffix; the older 64-vCPU metals are a bare ".metal".
    _sizes = {
        "medium": 1,
        "large": 2,
        "xlarge": 4,
        "2xlarge": 8,
        "4xlarge": 16,
        "8xlarge": 32,
        "12xlarge": 48,
        "16xlarge": 64,
        "24xlarge": 96,
        # bare-metal size tokens (e.g. r8g.metal-24xl → "metal-24xl")
        "metal-24xl": 96,
        "metal-48xl": 192,
        "metal": 64,  # c7g/m7g/r7g.metal etc. (Graviton2/3 64-vCPU metal)
    }
    # Mem (MiB) per vCPU by family first letter. i8g/i8ge are memory-rich
    # like r-family (768GiB/96vcpu = 8GiB/vcpu), so map "i" alongside "r".
    _mem_ratio = {"c": 2048, "m": 4096, "r": 8192, "i": 8192}

    def _host_capacity(itype):
        """(vcpu, mem_mb) for a virtual or bare-metal instance type.

        Phase 7 / #430: pool members may be DIFFERENT sizes. Each host's REAL
        capacity is what init-host.sh self-reports at boot (nproc +
        /proc/meminfo) and registers into the hosts table, so a smaller member
        registers its own smaller total and is never oversold. This helper is
        only a synth-time PLANNING estimate (scale-out headroom), computed from
        the primary type. Verified against `ec2 describe-instance-types` for the
        four-type pool: r8g.metal-24xl 96/786432, r7g.metal 64/524288,
        m8g.metal-24xl 96/393216, m7g.metal 64/262144 — all four match."""
        family, size = itype.split(".")[0], itype.split(".")[1]
        vcpu = _sizes[size]
        mem = vcpu * _mem_ratio[family[0]]
        return vcpu, mem

    _vcpu_total, _mem_total = _host_capacity(_itype)
    _avail_vcpu = _vcpu_total - CFG["host"]["reserved_vcpu"]
    _avail_mem = _mem_total - CFG["host"]["reserved_mem_mb"]
    _host_user_hook = _parse_user_hook(CFG, "host")
    _edge_user_hook = _parse_user_hook(CFG, "edge")

    if _host_user_hook is not None:
        host_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[
                    f"arn:{self.partition}:s3:::{_host_user_hook['bucket']}/{_host_user_hook['key']}"
                ],
            )
        )

    # Load scripts from userdata/ and inject config
    ud_dir = Path(__file__).parent.parent / "userdata"

    init_sh = (ud_dir / "init-host.sh").read_text()
    init_sh = init_sh.replace("{{ROOTFS_PREFIX}}", CFG["s3"]["rootfs_prefix"])
    # Mixed-instance-type: inject only the RESERVED headroom; init-host.sh
    # computes each host's real total from nproc + /proc/meminfo at boot and
    # subtracts this, so hosts of different sizes in one ASG each register
    # their TRUE capacity (the old {{AVAIL_VCPU}}/{{AVAIL_MEM}} baked one
    # size's total into every host → mis-sized hosts oversold/undersold).
    init_sh = init_sh.replace(
        "{{HOST_RESERVED_VCPU}}", str(CFG["host"]["reserved_vcpu"])
    )
    init_sh = init_sh.replace(
        "{{HOST_RESERVED_MEM}}", str(CFG["host"]["reserved_mem_mb"])
    )
    #
    # 为什么不能只靠 nproc + /proc/meminfo:那读到的是【真机可用】值,比标称小
    # 1.8-1.9%(固件/硬件保留:r8g 标称 768GiB 而 MemTotal 只有 754GiB),再扣
    # reserved_* 后调度器算出的 allocatable 就到不了按标称定义的容量目标
    # (1c2G 口径实测只到 375/250/187/124,而标称理论值是 384/256/192/128)。
    # 早先的做法是给每个 family 配一个补偿系数(mem 1.022/1.023/1.025/1.028),
    # 但那是四个必须手算的魔数 —— 每上一款机型都要重算一次,且极易算错
    # (2026-08-11 就因为用推算容量而非实测,让 r7g/m7g 少了 5 个和 3 个)。
    # 改为在 synth 时把标称规格烤进 userdata:host 用 IMDS 已取到的 instance_type
    # 查表注册,ratio 回到干净的 cpu=4.0 / mem=1.0,零 per-family 系数。
    #
    # 规格来源是本文件既有的 _host_capacity(_sizes × _mem_ratio),已对四机型
    # 逐一核对过与 `aws ec2 describe-instance-types` 一致。表里覆盖池内全部机型
    # (含单一 instance_type 的非混池场景);host 查不到自己的机型时回落
    # nproc+/proc/meminfo 自报(保留 Phase 7 的混池安全性,不会因表缺项就注册 0)。
    _spec_pool = list(
        dict.fromkeys(
            [(CFG.get("host", {}) or {}).get("instance_type") or _itype]
            + list((CFG.get("host", {}) or {}).get("instance_types") or [])
        )
    )
    _spec_lines = []
    for _t in _spec_pool:
        if not _t:
            continue
        try:
            _v, _m = _host_capacity(_t)
        except KeyError:
            # 未知 size/family token:不猜,留给 host 侧自报回落。
            continue
        _spec_lines.append(f"{_t} {_v} {_m}")
    init_sh = init_sh.replace("{{NOMINAL_SPECS}}", "\n".join(_spec_lines))
    # (_collect_stranding_stats)按 allocatable = total × ratio 计算,而 allocatable
    # 是搁浅判据的分母。不渲染的话它只能拿 os.environ 默认 1.0 → m8g 实测
    # allocatable_vcpu 报 96(真值 384)、stranded_vcpu 报 0(真值 192),扩容决策会
    # 误判"无搁浅"。同一份 config 值也进 Lambda env(lambdas.py:264),两侧必须同源:
    # 指标说的搁浅与调度器判定的搁浅得是同一件事。
    init_sh = init_sh.replace(
        "{{CPU_OVERCOMMIT_RATIO}}",
        str((CFG.get("host", {}) or {}).get("cpu_overcommit_ratio", 1.0)),
    )
    init_sh = init_sh.replace(
        "{{MEM_OVERCOMMIT_RATIO}}",
        str((CFG.get("host", {}) or {}).get("mem_overcommit_ratio", 1.0)),
    )
    init_sh = init_sh.replace(
        "{{OVERCOMMIT_BY_FAMILY}}",
        json.dumps(
            (CFG.get("host", {}) or {}).get("overcommit_by_family") or {},
            separators=(",", ":"),
        ),
    )
    init_sh = init_sh.replace("{{SUBNET_PREFIX}}", CFG["vm"]["subnet_prefix"])
    init_sh = init_sh.replace(
        "{{ROOTFS_OVERLAY_MB}}", str(CFG["vm"].get("rootfs_overlay_mb", 8192))
    )
    # DNAT 端口段上下界:host 位图上界(route_ops.py 读 platform.env)与下方渲染
    # SG tcp_range 的值同源(Property 2)——不存在 SG 放行到 15000、位图只肯分旧值的
    # 裂缝。_edge_port_low/high 在此算一次,SG 渲染处复用同一变量。
    _edge_port_low = int((CFG.get("edge", {}) or {}).get("dnat_port_low", 10000))
    _edge_port_high = int((CFG.get("edge", {}) or {}).get("dnat_port_high", 15000))
    init_sh = init_sh.replace("{{DNAT_PORT_LOW}}", str(_edge_port_low))
    init_sh = init_sh.replace("{{DNAT_PORT_HIGH}}", str(_edge_port_high))
    # /etc/platform.env,route_ops.py:48 读)。#222 加了 init-host.sh 的占位符
    # 却漏了这条渲染 → 真机 host-agent 起来 import route_ops 时
    # int("{{PORT_QUARANTINE_SECONDS}}") 抛 ValueError,host-agent 崩溃重启循环、
    # 整台 host 不可调度(2026-07-15 美东1 真机复现)。默认 20 与 route_ops.py
    init_sh = init_sh.replace(
        "{{PORT_QUARANTINE_SECONDS}}",
        str((CFG.get("edge", {}) or {}).get("port_quarantine_seconds", 20)),
    )
    # Source.data resolves CDK tokens while deploying the object. Hashing the
    # tokenized string at synth time would therefore bind the bootstrap to a
    # digest that cannot match the deployed bytes. Keep dynamic service URLs in
    # the small LaunchTemplate bootstrap and inherit them as environment values.
    init_sh = init_sh.replace(
        "{{AGENTCORE_GATEWAY_URL}}",
        "${OC_BOOTSTRAP_AGENTCORE_GATEWAY_URL:-none}",
    )
    init_sh = init_sh.replace(
        "{{AMP_REMOTE_WRITE_URL}}",
        "${OC_BOOTSTRAP_AMP_REMOTE_WRITE_URL:-none}",
    )
    # Hardcode the names (same pattern as edge's claw-logs{gsuffix} in the edge
    # userdata below) — build_observability runs after build_ha_edge, so
    # ctx.log_firehose_stream_name_* isn't set yet at template time. Names are
    # deterministic from _gsuffix and must match observability.py.
    _logging_enabled = bool((CFG.get("logging") or {}).get("enabled", False))
    init_sh = init_sh.replace("{{LOGGING_ENABLED}}", str(_logging_enabled).lower())
    init_sh = init_sh.replace("{{FB_STREAM_HOST}}", f"claw-logs-host{self._gsuffix}")
    init_sh = init_sh.replace("{{FB_STREAM_VM}}", f"claw-logs-vm{self._gsuffix}")
    # Balloon config
    balloon_cfg = CFG.get("balloon", {})
    init_sh = init_sh.replace(
        "{{BALLOON_ENABLED}}", str(balloon_cfg.get("enabled", False)).lower()
    )
    init_sh = init_sh.replace(
        "{{BALLOON_DEFLATE_ON_OOM}}",
        str(balloon_cfg.get("deflate_on_oom", True)).lower(),
    )
    init_sh = init_sh.replace(
        "{{BALLOON_STATS_INTERVAL}}",
        str(balloon_cfg.get("stats_polling_interval_s", 5)),
    )
    init_sh = init_sh.replace(
        "{{BALLOON_FREE_PAGE_REPORTING}}",
        str(balloon_cfg.get("free_page_reporting", True)).lower(),
    )
    init_sh = init_sh.replace(
        "{{BALLOON_MAX_INFLATE_RATIO}}",
        str(balloon_cfg.get("max_inflate_ratio", 0.4)),
    )
    init_sh = init_sh.replace(
        "{{BALLOON_MIN_GUEST_AVAILABLE_MB}}",
        str(balloon_cfg.get("min_guest_available_mb", 512)),
    )
    # 数据面走两级路由(CloudFront → ALB → OpenResty edge → DNAT → microVM:18789),
    # microVM 不再 dial 回 hub。init-host.sh 的 CLAW_HUB_URL/CLAW_HUB_WS env
    # 已删。
    # 五个值写进 /etc/platform.env,init-host.sh 起 host dnsmasq + ipset 基建、
    # launch-vm.sh source 后据此决定放行/DROP。默认 enabled=false → launch-vm 保持
    # 末尾 FORWARD ACCEPT(现状零变化);true → 切默认拒绝(静态 CIDR + FQDN ipset 放行 +
    # 末尾 DROP)。VPC CIDR 直接用 CDK 解析出的 vpc.vpc_cidr_block(不依赖 host IMDS),
    # 覆盖 hub 私网 tap IP / 堡垒机 LiteLLM / EKS ALB / VPC Endpoint 私网。
    # 改这里→重建 host 即继承,绝不热改运行中 VM。
    #
    # 运营输入格式白名单校验(synth 期 fail-loud):domains/cidrs 会渲染进 platform.env
    # 和 dnsmasq/iptables 规则,虽是可信 config.yml 输入,但含换行/分号/空格会多写一行
    # 变量或多注入一条指令。这里正则夹紧,不合法直接炸 synth,不把脏值烤进部署代码。
    _egress_domains = (sec_cfg.get("egress_allowlist_domains") or "").strip()
    _egress_cidrs = (sec_cfg.get("egress_allowlist_cidrs") or "").strip()
    _egress_dns_upstream = (sec_cfg.get("egress_dns_upstream") or "8.8.8.8").strip()
    _dom_re = re.compile(
        r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I
    )
    _cidr_re = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
    _ipv4_re = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
    for _d in [x.strip() for x in _egress_domains.split(",") if x.strip()]:
        if not _dom_re.match(_d):
            raise ValueError(
                f"security.egress_allowlist_domains 含非法域名 {_d!r}:只允许逗号分隔的 FQDN"
                "(字母数字/连字符/点),不得含空格/分号/斜杠/换行(#39 防注入)"
            )
    for _c in [x.strip() for x in _egress_cidrs.split(",") if x.strip()]:
        if not _cidr_re.match(_c):
            raise ValueError(
                f"security.egress_allowlist_cidrs 含非法 CIDR {_c!r}:只允许逗号分隔的 IPv4 CIDR"
                "(如 10.0.0.0/16)(#39 防注入)"
            )
    if not (
        _ipv4_re.match(_egress_dns_upstream) or _dom_re.match(_egress_dns_upstream)
    ):
        raise ValueError(
            f"security.egress_dns_upstream 非法 {_egress_dns_upstream!r}:只允许 IPv4 或 FQDN(#39)"
        )
    init_sh = init_sh.replace(
        "{{EGRESS_ALLOWLIST_ENABLED}}",
        str(sec_cfg.get("egress_allowlist_enabled", False)).lower(),
    )
    init_sh = init_sh.replace(
        "{{EGRESS_INCLUDE_VPC_CIDR}}",
        str(sec_cfg.get("egress_allowlist_include_vpc_cidr", True)).lower(),
    )
    # P7 真机实撞:self_managed/imported 模式下 vpc.vpc_cidr_block 是 CFN
    # Token,str.replace 进 init_sh 后经 S3 交付永不解析,host 上渲染成字面量
    # "${Token[TOKEN.879]}" 让 init-host.sh:185 语法炸、host 初始化 ABANDON。
    # CIDR 在 config 里就是字面量,直接取;default_vpc 走 from_lookup 是
    # synth 期具体值,保持原样。
    _net_cfg_for_cidr = CFG.get("network", {}) or {}
    _net_mode_for_cidr = _net_cfg_for_cidr.get("mode", "default_vpc")
    if _net_mode_for_cidr == "self_managed":
        _egress_vpc_cidr = (_net_cfg_for_cidr.get("self_managed") or {}).get(
            "cidr"
        ) or "10.20.0.0/20"
    elif _net_mode_for_cidr == "imported":
        _egress_vpc_cidr = (_net_cfg_for_cidr.get("imported") or {})["cidr"]
    else:
        _egress_vpc_cidr = vpc.vpc_cidr_block
    init_sh = init_sh.replace("{{EGRESS_VPC_CIDR}}", _egress_vpc_cidr)
    init_sh = init_sh.replace("{{EGRESS_ALLOWLIST_CIDRS}}", _egress_cidrs)
    init_sh = init_sh.replace("{{EGRESS_ALLOWLIST_DOMAINS}}", _egress_domains)
    init_sh = init_sh.replace("{{EGRESS_DNS_UPSTREAM}}", _egress_dns_upstream)
    # 一台 host 同时冷启的 VM 数不超过它,防批量 recover/多批 SSM fan-out 二次洪峰压垮 host。
    # 默认 30(与 Lambda 侧 DISPATCH_HOST_LAUNCH_CONCURRENCY 单一来源同读 vm.host_launch_slots)。
    try:
        _launch_slots = int((CFG.get("vm", {}) or {}).get("host_launch_slots", 30))
    except (TypeError, ValueError):
        _launch_slots = 30
    if _launch_slots < 1:
        _launch_slots = 30
    init_sh = init_sh.replace("{{OC_HOST_LAUNCH_SLOTS}}", str(_launch_slots))
    # backup-data.sh is pulled from S3 at runtime (uses the $ASSETS_BUCKET shell
    # var init-host.sh resolves from the AssetsBucket stack output).
    init_sh = init_sh.replace(
        "{{BACKUP_DATA_SCRIPT}}",
        "aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/backup-data.sh /home/ubuntu/backup-data.sh --region ${REGION}\n"
        "chmod +x /home/ubuntu/backup-data.sh && chown ubuntu:ubuntu /home/ubuntu/backup-data.sh",
    )

    host_agent_svc = (ud_dir / "host-agent.service").read_text()
    init_sh = init_sh.replace(
        "{{HOST_AGENT_SCRIPT}}",
        f"cat > /etc/systemd/system/host-agent.service << 'SVCEOF'\n{host_agent_svc}SVCEOF",
    )
    # Inlining, not `aws s3 cp`, is deliberate on three counts:
    #   1. provision's bytes become part of init-host.sh's sha256, which is the digest the
    #      changes the LT, so the "what will this host run" question has one answer.
    #   2. A boot-time fetch of provision would be a network dependency in the stage whose
    #      whole purpose is removing network dependencies from boot.
    #   3. Image Builder reads the same file from S3, so one source of truth serves both
    #      the bake path and the plain-AMI fallback.
    # The heredoc is quoted so provision's own $VARs survive verbatim.
    _provision_sh = (ud_dir / "provision-host.sh").read_text()
    if "{{" in _provision_sh:
        raise ValueError(
            "provision-host.sh must not contain template placeholders: it is baked into a "
            "fleet-wide AMI, so it can carry no per-deployment value"
        )
    init_sh = init_sh.replace(
        "{{PROVISION_SCRIPT}}",
        "cat > /opt/openclaw/provision-host.sh << 'PROVISIONEOF'\n"
        f"{_provision_sh}PROVISIONEOF\n"
        "chmod 0755 /opt/openclaw/provision-host.sh",
    )
    init_sh = init_sh.replace(
        "{{HOST_USER_HOOK}}",
        _render_user_hook("host", _host_user_hook, "${REGION}"),
    )

    # NOTE: assets/backup bucket names + backup CMK key id are no longer injected
    # here. init-host.sh resolves them at runtime from stack outputs (AssetsBucket
    # / BackupBucket / BackupCmkKeyId), with an IMDS-account deterministic fallback.
    # This removes the ~19 Fn::Join bucket tokens that, together with the 21KB
    # script, blew the hard 16KB EC2 user-data limit.

    # Keep the rendered init script out of EC2 user-data. BucketDeployment uses a
    # CDK file asset, so CloudFormation stages this object before the Host ASG is
    # allowed to launch. The content hash is part of the destination key: old
    # versions remain addressable for rollback and an init change necessarily
    # changes the LaunchTemplate user-data.
    if Token.is_unresolved(init_sh):
        raise ValueError(
            "rendered init-host.sh contains an unresolved CDK token; resolve it "
            "at Host runtime before computing the immutable asset digest"
        )
    _init_sha256 = hashlib.sha256(init_sh.encode("utf-8")).hexdigest()
    _init_key_prefix = f"deployment/bootstrap/host/{_init_sha256}"
    _init_key = f"{_init_key_prefix}/init-host.sh"
    _host_init_asset = s3deploy.BucketDeployment(
        self,
        "HostInitAssetDeployment",
        sources=[s3deploy.Source.data("init-host.sh", init_sh)],
        destination_bucket=assets_bucket,
        destination_key_prefix=_init_key_prefix,
        prune=False,
        retain_on_delete=True,
    )

    # Ubuntu does not guarantee awscli on a clean AMI, so the bootstrap installs
    # the architecture-correct CLI before reading the private S3 object. Never
    # pipe remote bytes into bash: download, verify the full digest, then exec.
    # A bootstrap failure settles the lifecycle hook as ABANDON when awscli is
    # available; init-host.sh installs its own EXIT trap after exec.
    _bootstrap = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "exec > >(tee /var/log/openclaw-bootstrap.log > /dev/console) 2>&1\n"
        'echo "[oc:bootstrap] starting immutable init-host download"\n'
        f'export OC_BOOTSTRAP_AMP_REMOTE_WRITE_URL="{amp_remote_write_url or "none"}"\n'
        f'export OC_BOOTSTRAP_AGENTCORE_GATEWAY_URL="{gateway_url or "none"}"\n'
        "_install_packages() {\n"
        "  export DEBIAN_FRONTEND=noninteractive\n"
        "  apt-get -o DPkg::Lock::Timeout=60 update -qq\n"
        "  apt-get -o DPkg::Lock::Timeout=60 install -y -qq \"$@\" >/dev/null\n"
        "}\n"
        "command -v curl >/dev/null 2>&1 || _install_packages ca-certificates curl\n"
        "TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token "
        "-H 'X-aws-ec2-metadata-token-ttl-seconds: 300')\n"
        'imds() { curl -fsS -H "X-aws-ec2-metadata-token: ${TOKEN}" '
        '"http://169.254.169.254/latest/meta-data/$1"; }\n'
        "REGION=$(imds placement/region)\n"
        "INSTANCE_ID=$(imds instance-id)\n"
        "_abandon() {\n"
        "  rc=$?; trap - EXIT\n"
        "  [ -z \"${tmp:-}\" ] || rm -f \"$tmp\" || true\n"
        "  [ -z \"${staged:-}\" ] || rm -f \"$staged\" || true\n"
        "  if [ \"$rc\" -ne 0 ] && command -v aws >/dev/null 2>&1; then\n"
        "    aws autoscaling complete-lifecycle-action "
        "--lifecycle-hook-name openclaw-host-init "
        "--auto-scaling-group-name openclaw-hosts-asg "
        "--lifecycle-action-result ABANDON "
        "--instance-id \"${INSTANCE_ID}\" --region \"${REGION}\" || true\n"
        "  fi\n"
        "  exit \"$rc\"\n"
        "}\n"
        "trap _abandon EXIT\n"
        "if ! command -v aws >/dev/null 2>&1; then\n"
        "  command -v unzip >/dev/null 2>&1 || _install_packages unzip\n"
        "  case \"$(uname -m)\" in\n"
        "    x86_64|amd64) AWSCLI_ARCH=x86_64 ;;\n"
        "    aarch64|arm64) AWSCLI_ARCH=aarch64 ;;\n"
        "    *) echo '[oc:bootstrap] FATAL: unsupported CPU architecture'; exit 1 ;;\n"
        "  esac\n"
        "  curl -fsSL \"https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}.zip\" "
        "-o /tmp/awscliv2.zip\n"
        "  (cd /tmp && unzip -qo awscliv2.zip && ./aws/install)\n"
        "fi\n"
        "tmp=$(mktemp /tmp/init-host.XXXXXX)\n"
        "for attempt in $(seq 1 20); do\n"
        f"  if aws s3 cp \"s3://{assets_bucket.bucket_name}/{_init_key}\" "
        "\"$tmp\" --region \"$REGION\" --no-progress; then break; fi\n"
        "  [ \"$attempt\" -lt 20 ] || exit 1\n"
        "  sleep 15\n"
        "done\n"
        f"printf '%s  %s\\n' '{_init_sha256}' \"$tmp\" | sha256sum -c -\n"
        "staged=/var/lib/cloud/.init-host.sh.$$\n"
        "install -o root -g root -m 0700 \"$tmp\" \"$staged\"\n"
        "mv -f \"$staged\" /var/lib/cloud/init-host.sh\n"
        "rm -f \"$tmp\"\n"
        'echo "[oc:bootstrap] verified init-host '
        f"{_init_sha256}, executing\"\n"
        "exec bash /var/lib/cloud/init-host.sh\n"
    )
    if len(_bootstrap.encode()) > 16384:
        raise ValueError(
            f"host bootstrap user-data {len(_bootstrap.encode())}B exceeds 16KB"
        )
    user_data = ec2.UserData.custom(_bootstrap)

    # AMI lookup — selects Ubuntu Noble for the configured CPU arch.
    # Graviton hosts (arch=arm64) need a *-arm64-server AMI; mismatched
    # AMI + instance type fails to boot, so we couple the two.
    _arch = (CFG.get("host", {}) or {}).get("arch", "x86_64")
    _ami_arch = "arm64" if _arch == "arm64" else "amd64"
    # SSM parameter OpenClawHostImage's pipeline writes, so hosts boot an image that
    # already has every component installed and download nothing.
    #
    # resolve:ssm, not a lookup: the value is resolved by EC2 at each launch, so a new bake
    # takes effect on the next scale-out with no cdk deploy. Per the EC2 docs, changing the
    # parameter does NOT touch running instances — that is K1 (no automatic instance
    # refresh) for free. A lookup would instead freeze today's AMI id into the template.
    #
    # Both paths run the same init-host.sh; the only difference is whether provision
    # already ran. Falling back to the plain Canonical AMI therefore stays safe.
    _golden = (CFG.get("host", {}) or {}).get("golden_ami", {}) or {}
    # None = 没走 resolve:ssm。tracker 的 IAM 要按这个分支决定是否授 ssm:GetParameters
    # (见下方 track_default_lt_version 调用处)。
    _golden_param = None
    if _golden.get("use", False):
        _golden_param = _golden.get("ssm_parameter") or host_golden_ami_parameter_name(
            self._gsuffix
        )
        ami = ec2.MachineImage.resolve_ssm_parameter_at_launch(_golden_param)
    else:
        ami = ec2.MachineImage.lookup(
            name=f"ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-{_ami_arch}-server-*",
            owners=["099720109477"],
        )

    # InstanceType: honor an explicit override, otherwise pick a sensible
    # default per arch (m8g for Graviton, m8i for Intel).
    _instance_type_str = (CFG.get("host", {}) or {}).get("instance_type") or (
        "m8g.xlarge" if _arch == "arm64" else "m8i.xlarge"
    )

    # config host.instance_types: optional list of types the ASG may pick
    # from (availability + Spot resilience). When given, the ASG runs a
    # MixedInstancesPolicy across them; the launch template declares the
    # primary type (pool's first entry). Phase 7: members may now be
    # DIFFERENT SIZES — init-host.sh self-reports each host's real capacity
    # (nproc + /proc/meminfo) at boot, so a smaller member registers its own
    # smaller total and is never oversold. The previous equal-capacity assert
    # is gone. Members must still share the SAME ARCH as the AMI (x86 vs
    # arm64 image is arch-specific); we assert that instead.
    _instance_pool = (CFG.get("host", {}) or {}).get("instance_types") or []
    if _instance_pool:
        # primary type comes from the pool's first entry
        _instance_type_str = _instance_pool[0]

        def _itype_is_arm(t):
            # Graviton families carry a 'g' in the family token
            # (m8g/c7g/r8g/i8g…); x86 (m8i/c7i/r7i…) do not.
            fam = t.split(".")[0]
            return "g" in fam

        _arch_is_arm = _arch == "arm64"
        _mismatched = [t for t in _instance_pool if _itype_is_arm(t) != _arch_is_arm]
        if _mismatched:
            raise ValueError(
                f"host.instance_types must all match host.arch={_arch} "
                f"(AMI is arch-specific); mismatched: {_mismatched}"
            )
        # Capacity for scaler math is taken from the PRIMARY type; each host's
        # real capacity is what init-host.sh registers, so a mixed pool is
        # fine — this is only a planning estimate for scale-out headroom.
        _vcpu_total, _mem_total = _host_capacity(_instance_type_str)
        _avail_vcpu = _vcpu_total - CFG["host"]["reserved_vcpu"]
        _avail_mem = _mem_total - CFG["host"]["reserved_mem_mb"]

    # Bare-metal hosts run Firecracker on the Nitro hardware's native KVM,
    # NOT nested virtualization. Per AWS docs, CpuOptions.NestedVirtualization
    # applies only to *virtual* (non-metal) instance types and would fail /
    # be ignored on metal. We gate the nested-virt CustomResource on this.
    _is_metal = ".metal" in _instance_type_str

    # 开发期 SSH(CLAUDE.md 铁律:开发用 SSH 不用 SSM)。config host.ssh_key_name
    # 配了就给 metal 绑 keypair,让堡垒机能 SSH 进去调试/起节点。生产留空=无 key。
    _host_key_name = (CFG.get("host", {}) or {}).get("ssh_key_name") or None
    # 私有子网模式下 host 不要公网 IP(默认 VPC 公有子网需要公 IP 出网,
    _host_net_mode = (CFG.get("network", {}) or {}).get("mode", "default_vpc")
    _host_assoc_pub_ip = (
        False if _host_net_mode in ("self_managed", "imported") else None
    )
    launch_template = ec2.LaunchTemplate(
        self,
        "HostLT",
        launch_template_name="openclaw-host-lt",
        instance_type=ec2.InstanceType(_instance_type_str),
        machine_image=ami,
        security_group=sg,
        role=host_role,
        user_data=user_data,
        key_name=_host_key_name,
        associate_public_ip_address=_host_assoc_pub_ip,
        block_devices=[
            ec2.BlockDevice(
                device_name="/dev/sda1",
                # Encrypt the root volume at rest too (AWS-managed KMS key),
                # so the host has no plaintext EBS volume. Defense-in-depth:
                # protects against EBS volume/snapshot exfiltration; does NOT
                # protect microVM disk files against an on-host root dd
                # (EBS encryption is transparent to host root).
                volume=ec2.BlockDeviceVolume.ebs(
                    CFG["host"]["root_volume_gb"],
                    volume_type=ec2.EbsDeviceVolumeType.GP3,
                    encrypted=True,
                ),
            ),
            ec2.BlockDevice(
                device_name="/dev/sdf",
                # Encrypt at rest with AWS-managed KMS key.
                # Tenant data (rootfs overlays, data volumes, backups in transit) live here.
                volume=ec2.BlockDeviceVolume.ebs(
                    CFG["host"]["data_volume_gb"],
                    volume_type=ec2.EbsDeviceVolumeType.GP3,
                    encrypted=True,
                    delete_on_termination=not CFG["host"].get(
                        "keep_data_volume", False
                    ),
                ),
            ),
        ],
    )

    cfn_lt = launch_template.node.default_child

    # ── 实例 tag:让 host 被 Prometheus ec2_sd 自动发现 ──
    # deploy/monitoring/prometheus.yml 的 ec2_sd_configs 按 tag:Project=openclaw +
    # tag:Role=metal-host 过滤发现 metal host 抓 :8899/metrics。CDK LaunchTemplate
    # 默认只给实例打 Name,不打这俩 tag → Prometheus 发现不到 host、采集断链
    # (实测 metal 实例只有 Name tag)。这里在 LaunchTemplateData 层加
    # TagSpecifications,让 ASG 起的每台 host(及其卷)都带这俩 tag,随重建继承。
    # key 大小写须与 prometheus.yml filters 完全一致(Project/Role 首字母大写)。
    _host_tags = [
        {"Key": "Project", "Value": "openclaw"},
        {"Key": "Role", "Value": "metal-host"},
    ]
    cfn_lt.add_property_override(
        "LaunchTemplateData.TagSpecifications",
        [
            {"ResourceType": "instance", "Tags": _host_tags},
            {"ResourceType": "volume", "Tags": _host_tags},
        ],
    )

    # ── SECURITY (defense-in-depth for IMDS): require IMDSv2 + hop-limit 1 ──
    # The primary guest→IMDS egress block is the iptables DROP in
    # launch-vm.sh; this hardens the host side. Requiring session tokens
    # (HttpTokens=required) kills IMDSv1 credential theft via simple SSRF,
    # and HttpPutResponseHopLimit=1 stops a process one network hop away
    # from obtaining a token. host-agent.py / the AWS SDK use the IMDSv2
    # flow, so this is transparent to legitimate callers.
    # (fd00:ec2::254),与 launch-vm.sh 的 per-tap disable_ipv6=1 一起把 IPv6
    # IMDS 一刀切断,不留 SSRF via IPv6 的通路。IMDSv6 是 opt-in,disable 是
    # AWS 明确记录的支持值(EC2 metadata options),默认关只是加固纪律。
    cfn_lt.add_property_override(
        "LaunchTemplateData.MetadataOptions",
        {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "HttpEndpoint": "enabled",
            "HttpProtocolIpv6": "disabled",
        },
    )

    if CFG["asg"].get("use_spot"):
        cfn_lt.add_property_override(
            "LaunchTemplateData.InstanceMarketOptions",
            {
                "MarketType": "spot",
                "SpotOptions": {"SpotInstanceType": "one-time"},
            },
        )

    # Pin a launch-template version via CustomResource. On VIRTUAL hosts
    # this also flips CpuOptions.NestedVirtualization=enabled (CFN can't set
    # it directly). On BARE-METAL hosts (Firecracker on native KVM) nested
    # virtualization is neither needed nor supported, so we OMIT that field
    # and the version only restates the IMDS hardening. The CustomResource
    # still runs on metal so the ASG pins a known version either way.
    _lt_data_override = {
        # Carry the IMDSv2/hop-limit hardening into the pinned version too.
        # CreateLaunchTemplateVersion merges onto SourceVersion=$Latest so
        # this would normally be inherited, but we restate it so the
        # security posture is explicit and cannot silently regress.
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "HttpEndpoint": "enabled",
            "HttpProtocolIpv6": "disabled",
        },
    }
    if not _is_metal:
        _lt_data_override["CpuOptions"] = {"NestedVirtualization": "enabled"}
    create_ver_call = cr.AwsSdkCall(
        service="EC2",
        action="createLaunchTemplateVersion",
        parameters={
            "LaunchTemplateId": launch_template.launch_template_id,
            "SourceVersion": "$Latest",
            "LaunchTemplateData": _lt_data_override,
        },
        physical_resource_id=cr.PhysicalResourceId.of(
            Fn.join(
                "-",
                [
                    "lt-version",
                    cfn_lt.ref,
                    Fn.get_att(cfn_lt.logical_id, "LatestVersionNumber").to_string(),
                ],
            )
        ),
        output_paths=["LaunchTemplateVersion.VersionNumber"],
    )
    nested_virt = cr.AwsCustomResource(
        self,
        "NestedVirt",
        on_create=create_ver_call,
        on_update=create_ver_call,
        install_latest_aws_sdk=True,
        policy=cr.AwsCustomResourcePolicy.from_statements(
            [
                iam.PolicyStatement(
                    actions=[
                        "ec2:CreateLaunchTemplateVersion",
                        "ec2:DescribeLaunchTemplateVersions",
                    ],
                    resources=["*"],
                ),
            ]
        ),
    )
    nested_virt.node.add_dependency(launch_template)

    set_default = cr.AwsCustomResource(
        self,
        "SetDefaultLTVersion",
        on_create=cr.AwsSdkCall(
            service="EC2",
            action="modifyLaunchTemplate",
            parameters={
                "LaunchTemplateId": launch_template.launch_template_id,
                "DefaultVersion": nested_virt.get_response_field(
                    "LaunchTemplateVersion.VersionNumber"
                ),
            },
            physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
        ),
        on_update=cr.AwsSdkCall(
            service="EC2",
            action="modifyLaunchTemplate",
            parameters={
                "LaunchTemplateId": launch_template.launch_template_id,
                "DefaultVersion": nested_virt.get_response_field(
                    "LaunchTemplateVersion.VersionNumber"
                ),
            },
            physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
        ),
        install_latest_aws_sdk=False,
        policy=cr.AwsCustomResourcePolicy.from_statements(
            [
                iam.PolicyStatement(
                    actions=["ec2:ModifyLaunchTemplate"], resources=["*"]
                ),
            ]
        ),
    )
    set_default.node.add_dependency(nested_virt)

    # 判据 4);default_vpc 兼容档回落 public(默认 VPC 没私有子网,存量部署
    # byte-identical)。短路 `or` 之前会让 self_managed 也吃 public——已修。
    _net_mode = (CFG.get("network", {}) or {}).get("mode", "default_vpc")
    # 子网场景);缺省回落按 network.mode 的私有/公有逻辑。
    _host_subnet_ids = (CFG.get("host", {}) or {}).get("subnet_ids") or []
    if _host_subnet_ids:
        _host_subnets = ec2.SubnetSelection(
            subnets=[
                ec2.Subnet.from_subnet_id(self, f"HostSubnet{_i}", _sid)
                for _i, _sid in enumerate(_host_subnet_ids)
            ]
        )
    elif _net_mode in ("self_managed", "imported"):
        _host_subnets = ec2.SubnetSelection(subnets=vpc.private_subnets[:_az_count])
    else:
        _host_subnets = ec2.SubnetSelection(
            subnets=vpc.public_subnets[:_az_count] or vpc.private_subnets[:_az_count]
        )
    asg = autoscaling.AutoScalingGroup(
        self,
        "HostASG",
        auto_scaling_group_name="openclaw-hosts-asg",
        vpc=vpc,
        vpc_subnets=_host_subnets,
        launch_template=launch_template,
        min_capacity=CFG["asg"]["min_capacity"],
        max_capacity=CFG["asg"]["max_capacity"],
    )
    asg.node.add_dependency(set_default)
    # The bootstrap cannot perform its own retry until init-host.sh exists.
    # Gate Host launch on the immutable object deployment instead of setup.sh's
    # post-CDK uploader, which is intentionally only for second-stage scripts.
    asg.node.add_dependency(_host_init_asset)
    # Golden-image bake is a separate, non-blocking stack (OpenClawImageStack)
    # now, so the ASG no longer depends on image readiness. On a fresh region the
    # first host may boot before the image is in S3 and churn a few minutes via
    # the lifecycle-hook timeout until the bake lands the rootfs — not a deploy
    # failure. Steady-state redeploys are unaffected (image already present).
    cfn_asg = asg.node.default_child
    # ModifyLaunchTemplate 翻默认版本就对下次 launch 生效,不碰存量在跑实例(K1)。
    # 但 CFN 【不能】把 `$Default` 写进模板(必填 + resource handler 阶段硬拒该字面值),
    # 所以这里给数字版本过 CFN 校验,再由下面的 TrackDefaultLTVersion CR 改成 `$Default`
    # —— 详见 _helpers.track_default_lt_version 的 docstring(含真机实测结论)。
    # SetDefaultLTVersion CR 已把 nested-virt 版本设为默认,故 `$Default` 天然带
    # nested-virt/IMDS 加固;set_default 依赖保证首次部署时默认版本已就绪再起 ASG。
    _pinned_ver = nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber")
    if len(_instance_pool) >= 2:
        # MixedInstancesPolicy across the equal-capacity pool. This property
        # is mutually exclusive with the plain LaunchTemplate property, so
        # we null that out and supply the LT ref under the mixed policy.
        _asg_override = {
            "MixedInstancesPolicy": {
                "LaunchTemplate": {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": launch_template.launch_template_id,
                        "Version": _pinned_ver,
                    },
                    # r8g.metal-24xl → r7g.metal → m8g.metal-24xl → m7g.metal,
                    # taken verbatim from config host.instance_types.
                    # Do NOT add a `Priority` key here: that property exists on
                    # AWS::EC2::SpotFleet's LaunchTemplateOverrides, NOT on the ASG's
                    # (CFN rejects it with "Unsupported property [Priority]" — real
                    # deploy failure, apse1 2026-08-10). For ASGs, "highest to lowest
                    # priority" == "first to last in the list", honored when
                    # OnDemandAllocationStrategy=prioritized.
                    "Overrides": [{"InstanceType": t} for t in _instance_pool],
                },
                # Capacity-optimized lowers Spot interruption by picking from
                # the deepest-capacity pools; on-demand portion honors
                # use_spot. Default: all on-demand unless use_spot sets a
                # spot percentage in config.
                #
                # Overrides in Priority order on scale-out (default is
                # lowest-price, which would ignore our ordering entirely and pick
                # whatever is cheapest — i.e. m7g first, the exact inverse of the
                # requirement). This governs which METAL TYPE gets launched;
                # which host a TENANT lands on is the scheduler's affinity
                # ranking (core.host_profile.affinity_tier). Both must agree or
                # the pool fills in one order and drains in another.
                # Spot keeps capacity-optimized: priority ordering on Spot raises
                # interruption risk, and use_spot defaults to false for hosts.
                "InstancesDistribution": {
                    "OnDemandBaseCapacity": CFG["asg"].get("on_demand_base", 0),
                    "OnDemandPercentageAboveBaseCapacity": (
                        0 if CFG["asg"].get("use_spot") else 100
                    ),
                    "OnDemandAllocationStrategy": "prioritized",
                    "SpotAllocationStrategy": "capacity-optimized",
                },
            }
        }
        cfn_asg.add_property_override("LaunchTemplate", None)
        cfn_asg.add_property_override(
            "MixedInstancesPolicy", _asg_override["MixedInstancesPolicy"]
        )
    else:
        _asg_override = {"LaunchTemplate.Version": _pinned_ver}
        cfn_asg.add_property_override("LaunchTemplate.Version", _pinned_ver)
    # 部署后把 Version 改成 `$Default`(CFN 写不进去,见上)。promote 的
    # _resolve_lt_id 硬要求字面 `$Default`,否则 409 拒绝执行。
    # asg_shape 必须带上上面 add_property_override 的内容:L1 getter 读不到 override,
    # 漏传会让"只改 instance pool / spot 比例"那类变更不触发 tracker 重跑。
    # ssm_image_parameter:golden_ami.use=true 时 LT 的 ImageId 是 resolve:ssm 占位符,
    # ASG 的 LT 授权预校验会去解析它,缺 ssm:GetParameters 必失败(见 _helpers 注释)。
    track_default_lt_version(
        self,
        "TrackDefaultLTVersion",
        asg,
        launch_template,
        host_role,
        asg_shape=_asg_override,
        ssm_image_parameter=_golden_param,
    )
    # Lifecycle hooks (standalone resources, not inline LifecycleHookSpecificationList)
    autoscaling.CfnLifecycleHook(
        self,
        "InitHook",
        auto_scaling_group_name=asg.auto_scaling_group_name,
        lifecycle_hook_name="openclaw-host-init",
        lifecycle_transition="autoscaling:EC2_INSTANCE_LAUNCHING",
        heartbeat_timeout=CFG["asg"]["lifecycle_hook_timeout"],
        default_result="ABANDON",
    )
    autoscaling.CfnLifecycleHook(
        self,
        "TerminateHook",
        auto_scaling_group_name=asg.auto_scaling_group_name,
        lifecycle_hook_name="openclaw-host-terminate",
        lifecycle_transition="autoscaling:EC2_INSTANCE_TERMINATING",
        heartbeat_timeout=120,
        default_result="CONTINUE",
    )

    # POST /bootstrap/promote 在【cdk deploy 已发布的 LT 版本】之间切默认版本:
    # DescribeLaunchTemplateVersions(枚举已发布版本、按 bootstrap sha 对账)+ ModifyLaunchTemplate
    # (把该 LT 的默认版本翻到目标已发布版本)。两台 ASG 都跟踪 `$Default`,EC2 每次 launch 解析默认
    # 版本,故翻默认 = 下次开机读那个版本的 bootstrap,不碰存量在跑实例(K1)。IAM 加在这里而不是
    # lambdas.py:LT/ASG 在 stack build 顺序里晚于 lambdas.py 才存在,照 :1225 的既有先例。
    #
    # 提权红线(codex 评审确认):API 【绝不】拿 CreateLaunchTemplateVersion / RunInstances /
    # PassRole / UpdateAutoScalingGroup。Create/RunInstances 能写任意 user-data/AMI(继承实例角色
    # → 任意代码),即便本代码只做版本切换也无法从 IAM 层保证;故彻底不授予。API 只能切到 cdk 部署
    # 过的版本(拿不到写脚本的能力)。ModifyLaunchTemplate 资源级死锁到本 host LT ARN。
    _host_lt_arn = self.format_arn(
        service="ec2", resource="launch-template",
        resource_name=launch_template.launch_template_id,
    )
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            # DescribeLaunchTemplateVersions 不支持资源级权限(AWS 文档明确),必须 "*" 无条件。
            # 纯只读,爆炸半径低(同既有 ec2_describe_policy 里的 Describe* 处置)。
            actions=["ec2:DescribeLaunchTemplateVersions"],
            resources=["*"],
        )
    )
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ec2:ModifyLaunchTemplate"],
            resources=[_host_lt_arn],
        )
    )

    # When a new host completes init → process pending tenants
    events.Rule(
        self,
        "HostReadyRule",
        event_pattern=events.EventPattern(
            source=["aws.autoscaling"],
            detail_type=["EC2 Instance Launch Successful"],
        ),
        targets=[targets.LambdaFunction(api_fn)],
    )

    # When a host is terminating → cleanup DynamoDB records
    events.Rule(
        self,
        "HostTerminateRule",
        event_pattern=events.EventPattern(
            source=["aws.autoscaling"],
            detail_type=["EC2 Instance-terminate Lifecycle Action"],
        ),
        targets=[targets.LambdaFunction(api_fn)],
    )

    # ========== AgentCore (optional, continued) ==========
    if ac_enabled:
        # Gateway already created above (before userdata processing)

        # Register Lambda tools on Gateway
        if ac_gateway and ac_cfg.get("gateway", {}).get("enabled", True):
            tools_fn = _lambda.Function(
                self,
                "AgentCoreTools",
                function_name="openclaw-agentcore-tools",
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="handler.lambda_handler",
                code=_lambda.Code.from_asset("deploy/lambda/agentcore_tools"),
                timeout=Duration.seconds(30),
                memory_size=2048,
            )
            ac_gateway.add_lambda_target(
                "tools",
                lambda_function=tools_fn,
                tool_schema=agentcore.ToolSchema.from_inline(
                    [
                        agentcore.ToolDefinition(
                            name="hello",
                            description="Say hello - test tool for verifying AgentCore Gateway connectivity",
                            input_schema=agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.OBJECT,
                                properties={
                                    "name": agentcore.SchemaDefinition(
                                        type=agentcore.SchemaDefinitionType.STRING,
                                        description="Name to greet",
                                    )
                                },
                            ),
                        ),
                        agentcore.ToolDefinition(
                            name="system_info",
                            description="Get Lambda runtime system information",
                            input_schema=agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.OBJECT
                            ),
                        ),
                        agentcore.ToolDefinition(
                            name="timestamp",
                            description="Get current UTC timestamp",
                            input_schema=agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.OBJECT,
                                properties={
                                    "format": agentcore.SchemaDefinition(
                                        type=agentcore.SchemaDefinitionType.STRING,
                                        description="iso or unix",
                                    )
                                },
                            ),
                        ),
                    ]
                ),
                gateway_target_name="openclaw-tools",
            )

        # Memory — persistent cross-session memory
        if ac_cfg.get("memory", {}).get("enabled", True):
            strategies = []
            for s in ac_cfg.get("memory", {}).get("strategies", ["semantic"]):
                if s == "semantic":
                    strategies.append(
                        agentcore.MemoryStrategy.using_semantic(
                            name="openclaw_semantic",
                            namespaces=["/openclaw/tenant/{actorId}/semantic"],
                        )
                    )
                elif s == "user_preference":
                    strategies.append(
                        agentcore.MemoryStrategy.using_user_preference(
                            name="openclaw_preferences",
                            namespaces=["/openclaw/tenant/{actorId}/preferences"],
                        )
                    )
            agentcore.Memory(
                self,
                "AgentCoreMemory",
                memory_name="openclaw_memory",
                description="OpenClaw per-tenant memory",
                expiration_duration=Duration.days(
                    ac_cfg.get("memory", {}).get("expiration_days", 90)
                ),
                memory_strategies=strategies,
            )

        # Code Interpreter — secure sandboxed Python execution
        if ac_cfg.get("code_interpreter", {}).get("enabled", True):
            agentcore.CodeInterpreterCustom(
                self,
                "AgentCoreCodeInterpreter",
                code_interpreter_custom_name="openclaw_code_interpreter",
            )

        # Browser — cloud-based web automation
        if ac_cfg.get("browser", {}).get("enabled", True):
            agentcore.BrowserCustom(
                self,
                "AgentCoreBrowser",
                browser_custom_name="openclaw_browser",
            )

        # Identity — workload identity for agent AWS access
        agentcore_l1.CfnWorkloadIdentity(
            self,
            "AgentCoreIdentity",
            name="openclaw_identity",
        )

        # Policy — Cedar-based access control (configure via AgentCore console)
        # CfnPolicy requires PolicyEngine setup; deferred to console for initial deployment

        # Observability — enabled automatically via CloudWatch when Gateway/Memory are created

    # Pass AgentCore config to API Lambda
    if ac_enabled:
        api_fn.add_environment("AGENTCORE_ENABLED", "true")
        if gateway_url:
            api_fn.add_environment("AGENTCORE_GATEWAY_URL", gateway_url)

    # ╓─── [包A 数据面] owner=A ── ALB/CloudFront/CORS(入口,引用 api_fn/health_fn/assets_bucket)─╖
    # ========== ALB (Dashboard Proxy) ==========
    # ALB requires ≥2 subnets in different AZs (AWS API constraint).
    # The multi_az.az_count knob controls ASG fan-out; ALB independently
    # always claims max(2, az_count) subnets so single-AZ ASG mode still
    # produces a valid load balancer.
    _alb_az_count = max(2, _az_count)
    #   · alb.internal(bool):必须显式声明,不再从 api.mode 派生,避免 API 模式
    #     调整时静默翻转 ALB 的公网/内网形态。
    #   · alb.subnet_ids(可选 list):客户显式指定子网 id,用 from_subnet_id 导入;
    #     缺省回落现有 public/private 逻辑。
    _alb_cfg = CFG.get("alb", {}) or {}
    if "internal" not in _alb_cfg:
        raise ValueError(
            "config 缺 `alb.internal`。#423 起必须显式声明(不再从 api.mode 派生)—— "
            "隐式派生会让 api.mode 的改动静默翻转 ALB 的公网/内网形态。"
        )
    _alb_internal = bool(_alb_cfg["internal"])
    _alb_subnet_ids = _alb_cfg.get("subnet_ids") or []
    if _alb_subnet_ids:
        _alb_subnets = [
            ec2.Subnet.from_subnet_id(self, f"AlbSubnet{_i}", _sid)
            for _i, _sid in enumerate(_alb_subnet_ids)
        ]
    elif _alb_internal:
        _alb_subnets = vpc.private_subnets[:_alb_az_count]
    else:
        _alb_subnets = (
            vpc.public_subnets[:_alb_az_count] or vpc.private_subnets[:_alb_az_count]
        )
    alb = elbv2.ApplicationLoadBalancer(
        self,
        "DashboardALB",
        load_balancer_name="openclaw-dashboard",
        vpc=vpc,
        vpc_subnets=ec2.SubnetSelection(subnets=_alb_subnets),
        internet_facing=not _alb_internal,
        # P2b · INTERFACE-CONTRACT §6:数据面是 SSE 流式 + WS 长连,ALB 默认
        # idle_timeout=60s 会掐断 >1min 无字节的连接。设 3600s(ALB 硬上限 4000s
        # 内),与 OpenResty proxy_read/send_timeout 3600s 对齐。CloudFront origin
        # 由硬上限 180s 兜(§6 更新:CF 180s → ALB 3600s → OpenResty 3600s,WS
        # 长静默 >180s 靠客户端 30s ping 兜)。
        idle_timeout=Duration.seconds(3600),
    )
    # 安全红线(design decision the ops guide):ALB 入站绝不对 0.0.0.0/0 开放。
    # add_listener 默认 open=True 会给 ALB SG 加 0.0.0.0/0:80 —— 必须 open=False,
    # 再显式放行 VPC CIDR;公网 ALB 额外放行 CloudFront origin-facing prefix
    # list。prefix list id 各区不同,从 context 读(cdk deploy 时
    # -c cf_origin_facing_prefix_list=pl-xxxx;ap-southeast-1=pl-31a34658)。
    listener = alb.add_listener(
        "HTTP",
        port=80,
        open=False,  # 不自动开 0.0.0.0/0
        default_action=elbv2.ListenerAction.fixed_response(
            404, content_type="text/plain", message_body="not found"
        ),
    )
    _CF_PL_BY_REGION = {
        "ap-southeast-1": "pl-31a34658",
        "us-east-1": "pl-3b927c52",
        "us-west-2": "pl-82a045eb",
    }
    # cloudfront.enabled 默认 false 后,公网 ALB 若只放 CloudFront prefix list 会
    # 变成公网可达但 SG 全拒的死门。公网访问者 CIDR 由运营员部署后按需追加,
    # 绝不放行 0.0.0.0/0。
    for _p in (80, 443):
        alb.connections.allow_from(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(_p),
            "ALB baseline: VPC CIDR only (no 0.0.0.0/0, #423)",
        )
    if not _alb_internal:
        # 公网 ALB 启用 CloudFront 回源时,额外放行 origin-facing managed
        # prefix list;保留 region 映射与 context 覆盖逻辑。
        # CloudFront origin-facing managed prefix list,按 region 映射(context 可覆盖)。
        # 之前只从 context 读,不传会让 CloudFront 回源被 SG 拒 → /hub 504
        # (重建实撞:必须手动补 pl 才通)。给常用 region 内置默认值让一键部署即可用。
        # ap-southeast-1=pl-31a34658 已真机实测放行后 CloudFront→ALB 通;其余 region 值
        # 若未列,部署时传 -c cf_origin_facing_prefix_list=<pl-id>。
        _cf_pl = self.node.try_get_context(
            "cf_origin_facing_prefix_list"
        ) or _CF_PL_BY_REGION.get(self.region)
        if _cf_pl:
            listener.connections.allow_default_port_from(
                ec2.Peer.prefix_list(_cf_pl),
                "CloudFront origin-facing only (no 0.0.0.0/0)",
            )
    # 已随 channel/hub 下线一并删除。数据面走 EdgeTargetGroup(OpenResty)
    # → DNAT → microVM:18789,由 P2b-iac 已建。/hub/* CloudFront behavior 也已删。
    alb.connections.allow_to(
        ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(80), "ALB to host Nginx"
    )
    sg.add_ingress_rule(
        ec2.Peer.security_group_id(
            alb.connections.security_groups[0].security_group_id
        ),
        ec2.Port.tcp(80),
        "ALB to Nginx",
    )
    sg.add_ingress_rule(
        ec2.Peer.ipv4(vpc.vpc_cidr_block),
        ec2.Port.tcp(80),
        "VPC to Nginx (ALB IP target health check)",
    )
    # host-agent 在 :8899 暴露 /health + Prometheus /metrics(host-agent.py:1091
    # 绑 0.0.0.0:8899)。deploy/monitoring 的 Prometheus EC2(同 VPC,独立 SG)按
    # tag 发现 host 后抓 :8899/metrics —— 但 host SG 此前只放行 22/80,8899 入站没开,
    # 抓取会被挡(prometheus.yml 注释「host 入站 SG 反过来放行本机」这条要求此前没
    # 在 CDK 落地)。放行 8899 给 VPC CIDR(host-agent 只私网可达,同 80 健康检查的
    # VPC 内放行模式),让监控采集链通,随重建继承。绝不对 0.0.0.0/0 开。
    sg.add_ingress_rule(
        ec2.Peer.ipv4(vpc.vpc_cidr_block),
        ec2.Port.tcp(8899),
        "VPC to host-agent :8899 (Prometheus /metrics scrape + /health)",
    )

    # Pass ALB info to API Lambda for path-based routing
    api_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
    api_fn.add_environment("VPC_ID", vpc.vpc_id)
    # 1.3.1: health_check Lambda needs ALB listener for AZ failover
    # to repoint /vm/<tenant_id>* rules across hosts.
    health_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
    # 1.4.2 (#fake-failover fix): the failover gate verifies the
    # tenant's dashboard URL is genuinely reachable through the public
    # path (ALB → nginx → VM) before flipping DDB to running. We use
    # the ALB DNS directly because (a) it's already public, (b) it
    # bypasses the CloudFront cache, (c) no extra DNS hop. The probe
    # hits http://<alb_dns>/vm/<tenant_id>/ — same path CloudFront
    # would forward to anyway.
    health_fn.add_environment("PUBLIC_BASE_URL", f"http://{alb.load_balancer_dns_name}")
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:CreateTargetGroup",
                "elasticloadbalancing:DeleteTargetGroup",
                "elasticloadbalancing:RegisterTargets",
                "elasticloadbalancing:DeregisterTargets",
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:DeleteRule",
                "elasticloadbalancing:ModifyRule",
                "elasticloadbalancing:DescribeRules",
                "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:DescribeListeners",
            ],
            resources=["*"],
        )
    )

    # config-gated 段。存量部署(redis.enabled=false + edge.enabled=false)
    # 零新资源、synth byte-identical。P7 部署新数据面前把两开关翻 true。
    _redis_cfg = CFG.get("redis", {}) or {}
    _edge_cfg = CFG.get("edge", {}) or {}
    redis_endpoint: str | None = None
    if _redis_cfg.get("enabled", False):
        # ── ElastiCache Multi-AZ Redis(§8)──
        # cluster mode disabled 单 shard:1 primary + N replica 跨 3 AZ;
        # automatic_failover + multi_az;primary endpoint DNS 下发到 host/edge。
        # SG:host_asg 写路由、edge_asg 读路由,都需 6379 出站到本 SG。
        _redis_sg = ec2.SecurityGroup(
            self,
            "RedisSG",
            vpc=vpc,
            security_group_name="openclaw-redis-sg",
            description="ElastiCache Redis (route table for edge two-tier routing)",
            allow_all_outbound=False,
        )
        # host_asg SG → Redis 6379(host-agent 双写)
        _redis_sg.add_ingress_rule(
            ec2.Peer.security_group_id(sg.security_group_id),
            ec2.Port.tcp(6379),
            "host-agent writes route:{tenant_id}",
        )
        # 私有子网场景);缺省回落 PRIVATE_WITH_EGRESS → private_subnets。
        #
        # #499 A2 — 这里【故意】没有 PRIVATE_ISOLATED 分支,记录现行契约免得再被问:
        #   · `_helpers.py` 那层 Database(PRIVATE_ISOLATED)子网仍然会建,只是不会被自动选中。
        #     所以今天要让 Redis/Valkey 落隔离层 = 显式写 `redis.subnet_ids`。
        #   · 不在本处恢复默认,是因为改默认对【在役】环境是破坏性的:部署态子网组一旦与
        #     synth 算出的集合不同,CFN 下发 ModifyCacheSubnetGroup 会被 ElastiCache 拒绝
        #     (不允许改被在役 replication group 占用的子网组)→ UPDATE_FAILED → 整栈回滚
        #     (2026-08-13 真机,#499 A1)。恢复默认属迁移动作,另立 issue 走。
        #   · 漂移已由 `scripts/preflight-check.sh` 的 Redis 子网组判据在部署前 BLOCK。
        _redis_subnet_ids_cfg = _redis_cfg.get("subnet_ids") or []
        if _redis_subnet_ids_cfg:
            _redis_subnets = list(_redis_subnet_ids_cfg)
        else:
            _redis_subnets = vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ).subnet_ids
            if not _redis_subnets:
                # default_vpc / imported 场景可能没有 PRIVATE_WITH_EGRESS,回落
                # private_subnets(imported 明确传的私有子网 id)。全空 fail-loud。
                _redis_subnets = [s.subnet_id for s in vpc.private_subnets]
        if len(_redis_subnets) < 2:
            raise ValueError(
                "redis.enabled=true 需要 ≥2 私有子网跨 AZ,当前 VPC 私有子网数="
                f"{len(_redis_subnets)}(网络模式改 self_managed 或 imported 传齐)"
            )
        # (不建新的、不改现有);否则按 subnet_ids 建新子网组。ARN 末段即 group name。
        # ⚠️ 复用者自保:该子网组须已覆盖 ≥2 AZ(automatic_failover+multi_az 要求),
        # 否则 CreateReplicationGroup 400(复用时绕过上面的 <2 fail-loud 校验)。
        _existing_sg_arn = str(_redis_cfg.get("existing_subnet_group_arn", "")).strip()
        if _existing_sg_arn:
            _redis_subnet_group = None
            _redis_subnet_group_name = _existing_sg_arn.split(":")[-1]
        else:
            _redis_subnet_group = elasticache.CfnSubnetGroup(
                self,
                "RedisSubnetGroup",
                description="OpenClaw route-table Redis (private subnets)",
                subnet_ids=_redis_subnets,
                cache_subnet_group_name=f"openclaw-redis-subnets{self._gsuffix}",
            )
            _redis_subnet_group_name = _redis_subnet_group.ref
        _replicas = int(_redis_cfg.get("num_replicas", 2))
        # 支持 engine="valkey";协议/端口 6379 与 Redis 客户端(host-agent redis-py +
        # edge lua-resty-redis)线级兼容,数据面 route.lua 读写无需改。
        # engine_version 传 major.minor(如 "7.2"),补丁号(7.2.6)由 AWS 托管、
        # 不显式指定 —— 依据 AWS ElastiCache 文档"Supported ElastiCache (Valkey)
        # versions"页:CreateReplicationGroup 的 EngineVersion 取 7.2 这类 major.minor
        # (docs.aws.amazon.com/AmazonElastiCache/latest/dg/supported-engine-versions.html)。
        # 默认 redis 向后兼容:存量 config.yml 无 engine 键 → 仍起 Redis,不触发
        # 已部署路由集群的 replacement。config.yml.example 出厂默认 valkey(迁移到位)。
        _redis_engine = str(_redis_cfg.get("engine", "redis")).lower()
        if _redis_engine not in ("redis", "valkey"):
            raise ValueError(
                f"redis.engine={_redis_engine!r} 不支持;只能是 'redis' 或 'valkey'"
            )
        # engine_version 兜底按引擎给对应默认:valkey 最低 7.2(无 7.1),redis 用 7.1。
        # 防呆:engine=valkey 但漏 engine_version 时兜底 7.1 = 非法组合(ElastiCache
        # 无 Valkey 7.1),部署 400。只填 major.minor("7.2"),补丁号(如 7.2.6)由 AWS
        # 托管不显式指定(design decision)。
        _redis_default_ver = "7.2" if _redis_engine == "valkey" else "7.1"
        _redis_engine_version = str(
            _redis_cfg.get("engine_version") or _redis_default_ver
        )
        # 参数组 family:引擎 + major(如 redis7 / valkey7)。DoD 要求显式建、
        # 不用 default parameter group(便于后续调 maxmemory-policy 等)。
        _redis_major = _redis_engine_version.split(".")[0]
        _redis_pg_family = f"{_redis_engine}{_redis_major}"
        # (不建新的、不改现有参数);否则按 family 新建。ARN 末段即 group name。
        # ⚠️ 复用者自保:该参数组 family 须匹配 engine+major(valkey7 / redis7),
        # 否则 CreateReplicationGroup 400(拿 redis7 参数组配 valkey 引擎会被拒)。
        _existing_pg_arn = str(
            _redis_cfg.get("existing_parameter_group_arn", "")
        ).strip()
        if _existing_pg_arn:
            _redis_param_group = None
            _redis_param_group_name = _existing_pg_arn.split(":")[-1]
        else:
            _redis_param_group = elasticache.CfnParameterGroup(
                self,
                "RouteRedisParamGroup",
                cache_parameter_group_family=_redis_pg_family,
                description=(
                    f"OpenClaw route table {_redis_engine} {_redis_engine_version} "
                    "(explicit param group, #271)"
                ),
            )
            _redis_param_group_name = _redis_param_group.ref
        _redis_rg = elasticache.CfnReplicationGroup(
            self,
            "RouteRedis",
            replication_group_description="OpenClaw tenant to host route table",
            engine=_redis_engine,
            engine_version=_redis_engine_version,
            cache_parameter_group_name=_redis_param_group_name,
            cache_node_type=str(_redis_cfg.get("node_type", "cache.r7g.large")),
            num_cache_clusters=1 + _replicas,
            automatic_failover_enabled=True,
            multi_az_enabled=True,
            # cluster mode disabled(单 shard 主从),用 primary endpoint。
            cache_subnet_group_name=_redis_subnet_group_name,
            security_group_ids=[_redis_sg.security_group_id],
            port=6379,
            # 显式关 transit 加密(design decision):SG 隔离即够(私网内 6379 只对 host/edge
            # SG),不开 TLS/auth_token —— 拉长部署链、host-agent redis-py +
            # lua-resty-redis 都要额外配。显式 False 优于隐式(防未来引擎默认变化)。
            transit_encryption_enabled=False,
        )
        if _redis_subnet_group is not None:
            _redis_rg.add_dependency(_redis_subnet_group)
        if _redis_param_group is not None:
            _redis_rg.add_dependency(_redis_param_group)
        redis_endpoint = (
            f"{_redis_rg.attr_primary_end_point_address}:"
            f"{_redis_rg.attr_primary_end_point_port}"
        )
        # host_asg 环境变量(占位符 replace 已过,只能走 SSM Parameter Store 让
        # init-host 从 SSM 读)。存量 init-host.sh 尚未读它,是 P3 阶段的对接;
        # 这里先把 endpoint 写 SSM,与 host-agent.py 读取端(ENGINE_REDIS_ENDPOINT
        # 环境变量)由 P3 launch-vm/init-host 桥接。
        ssm.StringParameter(
            self,
            "RedisEndpointParam",
            parameter_name="/openclaw/engine/redis/primary-endpoint",
            string_value=redis_endpoint,
            description="ElastiCache primary endpoint (read by host-agent and edge ASG)",
        )

    if _edge_cfg.get("enabled", False):
        # 前置校验:开 edge 必须开 Redis(edge OpenResty 靠 Redis 查路由)。
        if not _redis_cfg.get("enabled", False):
            raise ValueError(
                "edge.enabled=true requires redis.enabled=true "
                "(OpenResty edge 查 Redis 取 tenant_id→host:port,契约 §1/§2)"
            )
        # ── EdgeTargetGroup(LOR + interval=10s + /healthz warmup)──
        # 不显式命名:TG 换端口/协议要 replacement,显式名让 CFN 无法
        # 先建后删(AlreadyExists,P7 真机实撞 80→8080 更新回滚)。控制面
        # edge_admin 经 ASG TargetGroupARNs 反查,不依赖名字。
        edge_tg = elbv2.ApplicationTargetGroup(
            self,
            "EdgeTargetGroup",
            vpc=vpc,
            # nginx.conf:122 listen 8080(EDGE_LISTEN_PORT 默认),TG 打错 :80
            # 是 P7 真机 unhealthy 根因之一。
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.INSTANCE,
            load_balancing_algorithm_type=elbv2.TargetGroupLoadBalancingAlgorithmType.LEAST_OUTSTANDING_REQUESTS,
            deregistration_delay=Duration.seconds(15),
            health_check=elbv2.HealthCheck(
                path="/healthz",
                healthy_http_codes="200",
                interval=Duration.seconds(10),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
        )
        # path-pattern rule(而非提为 default fixed_response 404)因 ALB 只支持
        # 一条 default,保守放 rule 让日后有需要(如加内部管理 UI 挂 default)不冲突。
        listener.add_action(
            "EdgeRoute",
            priority=20,
            conditions=[elbv2.ListenerCondition.path_patterns(["/vm/*", "/ws/*"])],
            action=elbv2.ListenerAction.forward([edge_tg]),
        )

        # ── OpenResty edge ASG(独立,跨 3 AZ;ELB health check + grace 覆盖 warmup)──
        _edge_sg = ec2.SecurityGroup(
            self,
            "EdgeSG",
            vpc=vpc,
            security_group_name="openclaw-edge-sg",
            description="OpenResty edge ASG (ALB to edge :80; edge to Redis :6379)",
            allow_all_outbound=True,  # 出网装 openresty + 打 Redis
        )
        # ALB SG → edge:80(only)
        _edge_sg.add_ingress_rule(
            ec2.Peer.security_group_id(
                alb.connections.security_groups[0].security_group_id
            ),
            ec2.Port.tcp(8080),
            "ALB to OpenResty edge :8080 (only)",
        )
        # 沿用 host :8899 的 VPC CIDR 放行模式;8080 入站仍只属 ALB SG(数据面
        # 红线不动,metrics 不走数据面端口)。绝不对 0.0.0.0/0 开。
        # 经 MASQUERADE 后源=host VPC IP,VPC CIDR 放行无法区分租户/节点流量——
        # 8899 与 9145 一起收窄到 Prometheus SG 白名单归后续 issue,本条不预做。
        _edge_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(9145),
            "VPC to edge nginx :9145 (Prometheus /metrics scrape, #387)",
        )
        # edge SG → host SG DNAT 端口段(host 数据面入站的唯一来源)。
        # 安全红线(design decision):host 的数据面流量只能来自 OpenResty
        # edge 集群,别处一律拒。端口段上下界从 config edge.dnat_port_{low,high} 读
        # (默认 [10000,15000] = 5001 槽);此处复用上方算好的 _edge_port_{low,high},
        # 与注入 host 位图的 DNAT_PORT_{LOW,HIGH} 同源(Property 2,防两边裂开)。
        # 关闭(stopped)租户保留路由不回收端口,累计租户多时按需在 config 抬高上界。
        sg.add_ingress_rule(
            ec2.Peer.security_group_id(_edge_sg.security_group_id),
            ec2.Port.tcp_range(_edge_port_low, _edge_port_high),
            "edge to host DNAT port range (INTERFACE-CONTRACT S3)",
        )
        # 安全红线(design decision):禁止 host↔host 互访,堵跨租户/跨 host
        # 横向移动。旧 HostToHostDnatIngress(host SG 自引用放行 DNAT 端口段)
        # 是"edge 曾装在 host 上"旧布局的遗留;现在 OpenResty 是独立 ASG,
        # 数据面链路是 edge→host DNAT→microVM,host 之间无需互通该端口段。
        # 删掉这条自引用后,host 数据面入站只接受来自 edge SG 的流量。
        # route.lua balancer 的"他机分支"源头是 edge(非 host),不受影响;
        # dev 小布局"edge collapse 到 host"走同 SG 本就通,也不受影响。
        # edge SG → Redis 6379(读路由)
        if _redis_cfg.get("enabled", False):
            _redis_sg.add_ingress_rule(
                ec2.Peer.security_group_id(_edge_sg.security_group_id),
                ec2.Port.tcp(6379),
                "OpenResty edge reads route:{tenant_id}",
            )
        # launch template + user_data:注入 ENGINE_REDIS_ENDPOINT 环境变量给
        # install-edge.sh(deploy/edge/install-edge.sh:10-34 明确读它)。
        # user_data 走 shell + curl 拉 install-edge.sh 从 assets bucket(P3 补 asset
        # 上传);当前先内联占位,把 endpoint 写 systemd env file 让 install-edge 读。
        # P7 真机实撞:占位 userdata 不装 OpenResty → nginx 不监听 → ELB
        # unhealthy → ASG 无限换机。真自举:从 assets bucket 拉 deploy/edge/
        # 全套(setup.sh 上传到 deployment/edge/),跑 install-edge.sh。
        # 轮询等资产(EC2 先起、setup.sh 后传的竞态,同 LiteLLM userdata 套路)。
        _edge_role = iam.Role(
            self,
            "EdgeRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )
        assets_bucket.grant_read(_edge_role)
        if _edge_user_hook is not None:
            _edge_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[
                        f"arn:{self.partition}:s3:::{_edge_user_hook['bucket']}/{_edge_user_hook['key']}"
                    ],
                )
            )
        # #479:挂到 ctx 供 observability.py 的 edge Firehose 最小补授点使用。
        ctx.edge_role = _edge_role
        # 旧形态是 `aws s3 cp --recursive` 拉可变前缀 deployment/edge/,由 setup.sh
        # 手工上传,再 60×10s 轮询等 install-edge.sh 出现。两种真实故障出自这里:
        # 目录被当完整集消费,因为没有任何东西校验这一整套。
        #
        # 现在整棵 deploy/edge/ 打成一个 tar.gz(base64 文本,因为 Source.data 收
        # 文本),整体一个 sha256,key 里带这个 sha256 —— 与 host 的
        # deployment/bootstrap/host/<sha>/init-host.sh 同一套语义。BucketDeployment
        # 让 CloudFormation 在 edge ASG 允许启动之前就把对象放好,轮询本来就是为了
        # 绕这个竞态,竞态没了,轮询跟着删。
        #
        # 已知副作用(H1 决策时明确接受):改 deploy/edge/ 不再"改 S3 即生效",
        # 必须走 cdk deploy 或块 5 的版本切换 API。
        _edge_bundle, _edge_bundle_sha256 = build_edge_bundle(
            Path(__file__).parent.parent / "edge"
        )
        _edge_key_prefix = f"deployment/bootstrap/edge/{_edge_bundle_sha256}"
        _edge_key = f"{_edge_key_prefix}/{EDGE_BUNDLE_OBJECT_NAME}"
        _edge_bundle_asset = s3deploy.BucketDeployment(
            self,
            "EdgeBundleAssetDeployment",
            sources=[s3deploy.Source.data(EDGE_BUNDLE_OBJECT_NAME, _edge_bundle)],
            destination_bucket=assets_bucket,
            destination_key_prefix=_edge_key_prefix,
            prune=False,
            retain_on_delete=True,
        )
        # 解到 /opt/openclaw-edge/<sha>/ 而不是固定目录:每个版本独占一个目录,
        # 新旧文件在盘上物理上不可能混在一起(旧 --recursive 会把删掉的文件留下),
        # 重跑同一版本是同字节覆盖同路径 → 天然幂等。install-edge.sh 用
        # `dirname "$0"` 定位 route.lua / lib/ / nginx.conf / fluent-bit/edge,
        # tar 保留了这个相对结构,所以子目录对它透明。
        _edge_dir = f"/opt/openclaw-edge/{_edge_bundle_sha256}"
        # edge 没有 lifecycle hook(失败语义是 exit 1 → ELB unhealthy → ASG 换机),
        # 所以这里不需要 host 那样的 ABANDON 陷阱,只要失败得响。
        _edge_ud = ec2.UserData.for_linux()
        _edge_commands = [
            "set -euxo pipefail",
            # ENGINE_REDIS_ENDPOINT 需在 install-edge.sh 执行时可见(userdata
            # 属 CFN 模板,Redis endpoint token 在 deploy 期正确解析)。
            f'echo "ENGINE_REDIS_ENDPOINT={redis_endpoint}" >> /etc/environment',
            # AL2023 自带 awscli v2(AL2023 用户指南 "AWS CLI v2"),缺了就是 AMI
            # 被换过,继续跑只会在下一行报更难读的错。
            'command -v aws >/dev/null 2>&1 || { echo "[oc:edge-bootstrap] FATAL: '
            'awscli missing from AMI" >&2; exit 1; }',
            'tmp="$(mktemp /tmp/edge-bundle.XXXXXX)"',
            '_edge_cleanup() { rm -f "$tmp" || true; }',
            # 不带 exit:让 install-edge.sh 的退出码原样传给 cloud-init。
            "trap _edge_cleanup EXIT",
            f'for attempt in $(seq 1 20); do if aws s3 cp "s3://{assets_bucket.bucket_name}/{_edge_key}" '
            f'"$tmp" --region {self.region} --no-progress; then break; fi; '
            '[ "$attempt" -lt 20 ] || exit 1; sleep 15; done',
            # 摘要对的是 base64 文本本身,也就是实例真正下载到的字节;去校验里层
            # tar 会让传输编码这一段不设防。
            f"printf '%s  %s\\n' '{_edge_bundle_sha256}' \"$tmp\" | sha256sum -c -",
            f'install -d -o root -g root -m 0755 "{_edge_dir}"',
            f'base64 -d < "$tmp" | tar -xzf - -C "{_edge_dir}"',
            f'echo "[oc:edge-bootstrap] verified edge bundle {_edge_bundle_sha256}, installing"',
            f'ENGINE_REDIS_ENDPOINT="{redis_endpoint}" EDGE_LISTEN_PORT=8080 '
            f'LOGGING_ENABLED="{str(_logging_enabled).lower()}" '
            f'ASSETS_BUCKET="{assets_bucket.bucket_name}" '
            f'AWS_REGION="{self.region}" '
            f'FIREHOSE_DELIVERY_STREAM="claw-logs{self._gsuffix}" '
            f'bash "{_edge_dir}/install-edge.sh"',
        ]
        _edge_hook_command = _render_user_hook(
            "edge",
            _edge_user_hook,
            str(self.region),
            fail_cleanup="systemctl stop claw-edge.service 2>/dev/null || true",
        )
        if _edge_hook_command:
            _edge_commands.append(_edge_hook_command)
        _edge_ud.add_commands(*_edge_commands)
        _edge_lt = ec2.LaunchTemplate(
            self,
            "EdgeLaunchTemplate",
            launch_template_name=f"openclaw-edge-lt{self._gsuffix}",
            instance_type=ec2.InstanceType(
                str(_edge_cfg.get("instance_type", "c6in.xlarge"))
            ),
            # AL2023 x86_64 官方 AMI(Ubuntu 装 openresty 也可,统一 AL2023 与 host
            # 分开更省镜像烤/维护;install-edge.sh 里已 apt 装 openresty,
            # AL2023 用 dnf 装 openresty-signed,脚本按 os-release 分支)。
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            user_data=_edge_ud,
            security_group=_edge_sg,
            role=_edge_role,  # S3 拉 edge 资产 + SSM 运维通道
            associate_public_ip_address=False,  # 私有子网 + NAT 出网
        )
        # 照抄 host LT 的做法(上方 _host_tags 段):CDK LaunchTemplate 默认只给
        # 实例打 Name,不打 Project/Role → prometheus.yml 的 openclaw-edge-nginx
        # ec2_sd job 发现 0 target。必须打在 LaunchTemplateData.TagSpecifications
        # (ResourceType=instance)层——只给 LT 资源本身打标签对实例无效。
        # 注意:只对新起实例生效,存量 edge 需 instance refresh 或手工补 tag。
        _edge_tags = [
            {"Key": "Project", "Value": "openclaw"},
            {"Key": "Role", "Value": "edge"},
        ]
        _edge_cfn_lt = _edge_lt.node.default_child
        _edge_cfn_lt.add_property_override(
            "LaunchTemplateData.TagSpecifications",
            [
                {"ResourceType": "instance", "Tags": _edge_tags},
                {"ResourceType": "volume", "Tags": _edge_tags},
            ],
        )
        # 翻默认版本做临时切换;每次 cdk deploy 必须把默认版本【重置】回 CDK 当次发布的最新版本
        # (edge LT 每次 deploy 因 user-data 变化产生新版本),否则 API 的临时切换会永久盖过 IaC。
        # 这与 host 的 SetDefaultLTVersion CR 同一语义(host 那条把默认设成 nested-virt 版本)。
        # physical_resource_id 带 LatestVersionNumber → 版本变化时 CR 触发 on_update 重设默认。
        _edge_set_default = cr.AwsCustomResource(
            self,
            "EdgeSetDefaultLTVersion",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="modifyLaunchTemplate",
                parameters={
                    "LaunchTemplateId": _edge_lt.launch_template_id,
                    "DefaultVersion": Fn.get_att(
                        _edge_cfn_lt.logical_id, "LatestVersionNumber"
                    ).to_string(),
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    Fn.join("-", [
                        "edge-set-default",
                        Fn.get_att(
                            _edge_cfn_lt.logical_id, "LatestVersionNumber"
                        ).to_string(),
                    ])
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="EC2",
                action="modifyLaunchTemplate",
                parameters={
                    "LaunchTemplateId": _edge_lt.launch_template_id,
                    "DefaultVersion": Fn.get_att(
                        _edge_cfn_lt.logical_id, "LatestVersionNumber"
                    ).to_string(),
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    Fn.join("-", [
                        "edge-set-default",
                        Fn.get_att(
                            _edge_cfn_lt.logical_id, "LatestVersionNumber"
                        ).to_string(),
                    ])
                ),
            ),
            install_latest_aws_sdk=False,
            policy=cr.AwsCustomResourcePolicy.from_statements([
                # 资源级死锁到本 edge LT ARN(codex 评审:不能 "*",否则可重定向账户内任意 LT 默认版本)。
                iam.PolicyStatement(
                    actions=["ec2:ModifyLaunchTemplate"],
                    resources=[self.format_arn(
                        service="ec2", resource="launch-template",
                        resource_name=_edge_lt.launch_template_id,
                    )],
                ),
            ]),
        )
        _edge_set_default.node.add_dependency(_edge_lt)
        # PRIVATE_WITH_EGRESS(带 NAT 出网的私有子网)。
        _edge_subnet_ids = _edge_cfg.get("subnet_ids") or []
        if _edge_subnet_ids:
            _edge_subnets = [
                ec2.Subnet.from_subnet_id(self, f"EdgeSubnet{_i}", _sid)
                for _i, _sid in enumerate(_edge_subnet_ids)
            ]
        else:
            _edge_subnets = (
                vpc.select_subnets(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ).subnets
                or vpc.private_subnets
            )
        _edge_asg = autoscaling.AutoScalingGroup(
            self,
            "EdgeASG",
            auto_scaling_group_name="openclaw-edge-asg",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=_edge_subnets),
            launch_template=_edge_lt,
            min_capacity=int(_edge_cfg.get("min_capacity", 3)),
            max_capacity=int(_edge_cfg.get("max_capacity", 6)),
            # ELB(不是 EC2)health check:ALB /healthz 判定失败 → ASG terminate + 拉新
            health_check=autoscaling.HealthCheck.elb(
                grace=Duration.seconds(
                    int(_edge_cfg.get("health_check_grace_period_seconds", 300))
                )
            ),
        )
        # 同 HostASG:bootstrap 只能重试下载,不能凭空造出对象。把 edge 启动挡在
        # 不可变对象部署之后,这正是删掉 60×10s 轮询后必须补上的那条保证。
        _edge_asg.node.add_dependency(_edge_bundle_asset)
        # 也挡在 EdgeSetDefaultLTVersion 之后:$Default 必须先被设成本次 CDK 版本,edge 才起
        # (否则首启可能读到过期默认版本)。
        _edge_asg.node.add_dependency(_edge_set_default)
        _edge_asg.attach_to_application_target_group(edge_tg)
        # ModifyLaunchTemplate 翻默认版本即对 edge 下次 launch 生效。CFN 写不进
        # `$Default`,故模板给数字版本(LatestVersionNumber,即本次 deploy 发布的版本),
        # 再由 EdgeTrackDefaultLTVersion CR 改成 `$Default`。默认版本本身随 cdk deploy
        # 由上面的 EdgeSetDefaultLTVersion CR 重置回 CDK 当次发布版本。
        _edge_cfn_asg = _edge_asg.node.default_child
        _edge_asg_override = {
            "LaunchTemplate.Version": Fn.get_att(
                _edge_cfn_lt.logical_id, "LatestVersionNumber"
            ).to_string()
        }
        _edge_cfn_asg.add_property_override(
            "LaunchTemplate.Version", _edge_asg_override["LaunchTemplate.Version"]
        )
        # asg_shape 同 host:带上 override 内容,L1 getter 读不到它。
        track_default_lt_version(
            self,
            "EdgeTrackDefaultLTVersion",
            _edge_asg,
            _edge_lt,
            _edge_role,
            asg_shape=_edge_asg_override,
        )

        # 只需在 edge LT 上 ModifyLaunchTemplate(翻默认版本到已发布版本)。【不】要
        # CreateLaunchTemplateVersion / RunInstances / PassRole / UpdateAutoScalingGroup —— promote
        # 只在 cdk 已发布版本间切默认,拿不到写 user-data 的能力。DescribeLaunchTemplateVersions
        # 已在 host 段以 "*" 授过一次(不支持资源级)。edge LT 名带 _gsuffix,故 service 从 ASG 反查。
        _edge_lt_arn = self.format_arn(
            service="ec2", resource="launch-template",
            resource_name=_edge_lt.launch_template_id,
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ec2:ModifyLaunchTemplate"],
                resources=[_edge_lt_arn],
            )
        )

    # ========== CloudFront ==========
    # The retired root console S3 origin is no longer part of CloudFront.
    # Configured via:
    #
    #   cloudfront:
    #     console_domain: "console.example.com"     # legacy dual-mode selector
    #     console_cert_arn: "arn:aws:acm:us-east-1:..."  # selector pair
    #     app_domain:     "app.example.com"         # per-tenant dashboards (ALB)
    #     app_cert_arn:   "arn:aws:acm:us-east-1:..."
    #
    # If both pairs are set → DUAL mode: only the ALB-origin app distribution
    # remains; the console is served by the internal BFF ALB.
    # If unset (or only legacy custom_domain set) → LEGACY single-distribution
    # mode, kept for backward-compat with v1.3.3 and earlier deployments.
    # 旧默认 true 会给内网/自管入口静默引入多余公网 CDN 资源。
    _cf_cfg_raw = CFG.get("cloudfront")
    if _cf_cfg_raw is None:
        raise ValueError(
            "config 缺 `cloudfront` 段。#423 起必须显式声明 cloudfront.enabled "
            "(内网/自管入口部署写 false;需要公网 CDN 写 true) —— 默认值曾是 true,"
            "静默建 CDN 会给内网部署引入多余资源,故改为显式声明。"
        )
    _cf_enabled = bool((_cf_cfg_raw or {}).get("enabled", False))
    if _cf_enabled:
        cf_cfg = CFG.get("cloudfront", {}) or {}
        # ----- DUAL mode candidates -----
        console_domain = (cf_cfg.get("console_domain") or "").strip()
        console_cert_arn = (cf_cfg.get("console_cert_arn") or "").strip()
        app_domain = (cf_cfg.get("app_domain") or "").strip()
        app_cert_arn = (cf_cfg.get("app_cert_arn") or "").strip()
        dual_mode = bool(
            console_domain and console_cert_arn and app_domain and app_cert_arn
        )
        # ----- LEGACY single-domain fallback -----
        custom_domain = (cf_cfg.get("custom_domain") or "").strip()
        acm_cert_arn = (cf_cfg.get("acm_cert_arn") or "").strip()

        if dual_mode:
            # ===== DUAL mode:保留 per-tenant dashboards 的 ALB origin distribution =====
            app_cf = cloudfront.Distribution(
                self,
                "AppCF",
                comment="OpenClaw Per-Tenant Dashboards",
                domain_names=[app_domain],
                certificate=acm.Certificate.from_certificate_arn(
                    self, "AppCert", app_cert_arn
                ),
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        alb.load_balancer_dns_name,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        http_port=80,
                        # P2b · CF origin read_timeout:CDK 校验上限 180s,但账号
                        # 配额 L-AECE9FA7 "Response timeout per origin" 默认 120s,
                        # 写 180 会 CreateDistribution 400(P7 真机实撞 2026-07-08)。
                        # 取 120s(账号默认可部署);要 180s 先提配额再调。SSE 场景
                        # 120s 内几乎必有 token 流出;WS 长静默走 CF 断,靠客户端
                        # 30s ping 兜(契约 §8)。
                        read_timeout=Duration.seconds(120),
                        keepalive_timeout=Duration.seconds(60),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                ),
                default_root_object="",
            )
            console_host = alb.load_balancer_dns_name
            dashboard_host = app_domain
            console_cf_id = ""
            app_cf_id = app_cf.distribution_id
            cf_distribution = app_cf
        else:
            # ===== LEGACY single-distribution mode =====
            domain_names = [custom_domain] if custom_domain else None
            certificate = (
                acm.Certificate.from_certificate_arn(self, "CustomCert", acm_cert_arn)
                if custom_domain and acm_cert_arn
                else None
            )

            cf_distribution = cloudfront.Distribution(
                self,
                "DashboardCF",
                comment="OpenClaw Dashboard (single-domain mode)",
                domain_names=domain_names,
                certificate=certificate,
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        alb.load_balancer_dns_name,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        http_port=80,
                        # P2b · CF origin read_timeout:CDK 校验上限 180s,但账号
                        # 配额 L-AECE9FA7 "Response timeout per origin" 默认 120s,
                        # 写 180 会 CreateDistribution 400(P7 真机实撞 2026-07-08)。
                        # 取 120s(账号默认可部署);要 180s 先提配额再调。SSE 场景
                        # 120s 内几乎必有 token 流出;WS 长静默走 CF 断,靠客户端
                        # 30s ping 兜(契约 §8)。
                        read_timeout=Duration.seconds(120),
                        keepalive_timeout=Duration.seconds(60),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                ),
                default_root_object="",
            )

            # Single mode: console and dashboard share the same host
            console_host = custom_domain or cf_distribution.distribution_domain_name
            dashboard_host = console_host
            console_cf_id = cf_distribution.distribution_id
            app_cf_id = cf_distribution.distribution_id
    else:
        # 是内网 DNS;客户自管 CDN 指到该 ALB)。cf_distribution=None → 下游
        # Cognito wiring 只在 custom_domain 真值时才 deref,故此处安全。
        cf_distribution = None
        console_host = alb.load_balancer_dns_name
        dashboard_host = alb.load_balancer_dns_name
        console_cf_id = ""
        app_cf_id = ""
        custom_domain = ""
        dual_mode = False

    # ╓─── [包C 控制面+工程化] owner=C ── Cognito 鉴权 + Outputs(引用 cf_distribution/api_fn)─╖

    # --- Pack onto ctx ---
    ctx.alb = locals().get("alb")
    ctx.app_cf_id = locals().get("app_cf_id")
    ctx.asg = locals().get("asg")
    ctx.cf_distribution = locals().get("cf_distribution")
    ctx.console_cf_id = locals().get("console_cf_id")
    ctx.console_host = locals().get("console_host")
    ctx.custom_domain = locals().get("custom_domain")
    ctx.dashboard_host = locals().get("dashboard_host")
    ctx.dual_mode = locals().get("dual_mode")
    ctx.launch_template = locals().get("launch_template")
    ctx.listener = locals().get("listener")
    ctx.m = locals().get("m")
    ctx.sg = locals().get("sg")
