# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import re
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
    custom_resources as cr,
    Duration,
    Fn,
)
from pathlib import Path


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

    # ========== Multi-AZ HA (issue #8) ==========
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
    # arch-aware default if config.yml omits instance_type (issue #19).
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
        All instances in a MixedInstancesPolicy pool must be the same
        capacity (host total_vcpu/total_mem is injected statically into
        the hosts table at init), so this is computed from the primary
        type and shared across the pool."""
        family, size = itype.split(".")[0], itype.split(".")[1]
        vcpu = _sizes[size]
        mem = vcpu * _mem_ratio[family[0]]
        return vcpu, mem

    _vcpu_total, _mem_total = _host_capacity(_itype)
    _avail_vcpu = _vcpu_total - CFG["host"]["reserved_vcpu"]
    _avail_mem = _mem_total - CFG["host"]["reserved_mem_mb"]

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
    init_sh = init_sh.replace(
        "{{AGENTCORE_GATEWAY_URL}}", gateway_url if gateway_url else "none"
    )
    init_sh = init_sh.replace("{{AMP_REMOTE_WRITE_URL}}", amp_remote_write_url)
    # #245 host Fluent Bit: logging gate + the two host-side stream names.
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
    # #187 P5 — hub 拨出端点 template 变量已随 channel/hub 数据面下线一并移除。
    # 数据面走两级路由(CloudFront → ALB → OpenResty edge → DNAT → microVM:18789),
    # microVM 不再 dial 回 hub。init-host.sh 的 CLAW_HUB_URL/CLAW_HUB_WS env
    # 已删。
    # #39 microVM 出网默认拒绝白名单(L4 tap 级 iptables egress allowlist)。
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

    # NOTE: assets/backup bucket names + backup CMK key id are no longer injected
    # here. init-host.sh resolves them at runtime from stack outputs (AssetsBucket
    # / BackupBucket / BackupCmkKeyId), with an IMDS-account deterministic fallback.
    # This removes the ~19 Fn::Join bucket tokens that, together with the 21KB
    # script, blew the hard 16KB EC2 user-data limit.

    # user-data is now a plain string (no CFN tokens). The post-injection script
    # is ~23KB, over the hard 16KB EC2 limit. Raw gzip bytes can't go in a CFN
    # template (binary → "Template contains invalid characters"), so we embed a
    # base64(gzip(...)) blob inside a tiny ASCII bootstrap that decodes, gunzips
    # and execs it. base64 is ASCII-safe for the template; gzip keeps it small
    # (~9KB gzipped → ~12.5KB base64, comfortably under 16KB).
    import base64 as _b64
    import gzip as _gzip
    import re as _re

    # user-data 逼近 16KB 硬限:打包进 user-data 前剥掉整行注释 + 空行(源文件保留
    # 可读性,只瘦打包体)。heredoc-aware:sysctl/nginx conf 等 heredoc 体内的 `#`
    # 是配置内容不是注释,必须原样保留;保 shebang(#!)。不改脚本语义。
    def _strip_for_userdata(script: str) -> str:
        out = []
        heredoc_delim = None
        for line in script.splitlines():
            if heredoc_delim is not None:
                out.append(line)
                if line.strip() == heredoc_delim:
                    heredoc_delim = None
                continue
            m = _re.search(r"<<[-']?([A-Za-z_][A-Za-z0-9_]*)", line)
            if m:
                heredoc_delim = m.group(1)
                out.append(line)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") and not stripped.startswith("#!"):
                continue
            out.append(line)
        return "\n".join(out) + "\n"

    init_sh = _strip_for_userdata(init_sh)
    _blob = _b64.b64encode(_gzip.compress(init_sh.encode("utf-8"), 9)).decode("ascii")
    _bootstrap = (
        "#!/bin/bash\n"
        "set -e\n"
        f"echo {_blob} | base64 -d | gunzip > /tmp/init-host.sh\n"
        "exec bash /tmp/init-host.sh\n"
    )
    if len(_bootstrap.encode()) > 16384:
        raise ValueError(
            f"host user-data {len(_bootstrap.encode())}B exceeds 16KB even gzipped; "
            "move init-host.sh body to S3 bootstrap"
        )
    user_data = ec2.UserData.custom(_bootstrap)

    # AMI lookup — selects Ubuntu Noble for the configured CPU arch.
    # Graviton hosts (arch=arm64) need a *-arm64-server AMI; mismatched
    # AMI + instance type fails to boot, so we couple the two.
    _arch = (CFG.get("host", {}) or {}).get("arch", "x86_64")
    _ami_arch = "arm64" if _arch == "arm64" else "amd64"
    ami = ec2.MachineImage.lookup(
        name=f"ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-{_ami_arch}-server-*",
        owners=["099720109477"],
    )

    # InstanceType: honor an explicit override, otherwise pick a sensible
    # default per arch (m8g for Graviton, m8i for Intel).
    _instance_type_str = (CFG.get("host", {}) or {}).get("instance_type") or (
        "m8g.xlarge" if _arch == "arm64" else "m8i.xlarge"
    )

    # ── Mixed instance pool (task #22) ───────────────────────────────
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

    # 开发期 SSH(the ops guide 铁律:开发用 SSH 不用 SSM)。config host.ssh_key_name
    # 配了就给 metal 绑 keypair,让堡垒机能 SSH 进去调试/起节点。生产留空=无 key。
    _host_key_name = (CFG.get("host", {}) or {}).get("ssh_key_name") or None
    # 私有子网模式下 host 不要公网 IP(默认 VPC 公有子网需要公 IP 出网,
    # 存量 byte-identical 保 None)。#119 暴露红线。
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
    # ( 实测 metal 实例只有 Name tag)。这里在 LaunchTemplateData 层加
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
    # #34 — HttpProtocolIpv6=disabled 关掉 host 侧 IMDS 的 IPv6 端点
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
        # #34 — HttpProtocolIpv6=disabled 与上面 override 保持一致。
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

    # host 子网:self_managed / imported 走私有(AWS 暴露红线,#119/aws-architect
    # 判据 4);default_vpc 兼容档回落 public(默认 VPC 没私有子网,存量部署
    # byte-identical)。短路 `or` 之前会让 self_managed 也吃 public——已修。
    _net_mode = (CFG.get("network", {}) or {}).get("mode", "default_vpc")
    # #272 — host.subnet_ids 非空时显式选 openclaw host 机器子网(imported 私有
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
    # Golden-image bake is a separate, non-blocking stack (OpenClawImageStack)
    # now, so the ASG no longer depends on image readiness. On a fresh region the
    # first host may boot before the image is in S3 and churn a few minutes via
    # the lifecycle-hook timeout until the bake lands the rootfs — not a deploy
    # failure. Steady-state redeploys are unaffected (image already present).
    cfn_asg = asg.node.default_child
    _pinned_ver = nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber")
    if len(_instance_pool) >= 2:
        # MixedInstancesPolicy across the equal-capacity pool. This property
        # is mutually exclusive with the plain LaunchTemplate property, so
        # we null that out and supply the LT ref under the mixed policy.
        cfn_asg.add_property_override("LaunchTemplate", None)
        cfn_asg.add_property_override(
            "MixedInstancesPolicy",
            {
                "LaunchTemplate": {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": launch_template.launch_template_id,
                        "Version": _pinned_ver,
                    },
                    "Overrides": [{"InstanceType": t} for t in _instance_pool],
                },
                # Capacity-optimized lowers Spot interruption by picking from
                # the deepest-capacity pools; on-demand portion honors
                # use_spot. Default: all on-demand unless use_spot sets a
                # spot percentage in config.
                "InstancesDistribution": {
                    "OnDemandBaseCapacity": CFG["asg"].get("on_demand_base", 0),
                    "OnDemandPercentageAboveBaseCapacity": (
                        0 if CFG["asg"].get("use_spot") else 100
                    ),
                    "SpotAllocationStrategy": "capacity-optimized",
                },
            },
        )
    else:
        cfn_asg.add_property_override("LaunchTemplate.Version", _pinned_ver)
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
    # #272 — internal ALB + 子网可选(imported 私有子网场景)。
    #   · alb.internal(bool):派生默认 api.mode=private → internal(控制台内网,
    #     不经公网 CloudFront 回源);其余 → internet-facing(向后兼容)。显式设
    #     alb.internal 优先于派生。ctx._api_mode 由 build_network_vpc 先跑设好,
    #     缺失时回落 CFG api.mode。
    #   · alb.subnet_ids(可选 list):客户显式指定子网 id,用 from_subnet_id 导入;
    #     缺省回落现有 public/private 逻辑。
    _alb_cfg = CFG.get("alb", {}) or {}
    _api_mode_for_alb = (
        getattr(ctx, "_api_mode", None)
        or str((CFG.get("api", {}) or {}).get("mode", "")).strip().lower()
    )
    _alb_internal = bool(_alb_cfg.get("internal", _api_mode_for_alb == "private"))
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
        # P2b · the data-plane contract:数据面是 SSE 流式 + WS 长连,ALB 默认
        # idle_timeout=60s 会掐断 >1min 无字节的连接。设 3600s(ALB 硬上限 4000s
        # 内),与 OpenResty proxy_read/send_timeout 3600s 对齐。CloudFront origin
        # 由硬上限 180s 兜(§6 更新:CF 180s → ALB 3600s → OpenResty 3600s,WS
        # 长静默 >180s 靠客户端 30s ping 兜)。
        idle_timeout=Duration.seconds(3600),
    )
    # 安全红线(design decision the ops guide):公网走 CloudFront→ALB,ALB 入站【只】允许
    # CloudFront origin-facing managed prefix list,绝不对 0.0.0.0/0 开放。
    # add_listener 默认 open=True 会给 ALB SG 加 0.0.0.0/0:80 —— 必须 open=False,
    # 再显式只放行 CloudFront prefix list。prefix list id 各区不同,从 context 读
    # (cdk deploy 时 -c cf_origin_facing_prefix_list=pl-xxxx;ap-southeast-1=pl-31a34658)。
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
    if _alb_internal:
        # #272 — internal ALB 不经公网 CloudFront 回源,CloudFront prefix list
        # 无意义(那是公网回源用)。改放行 VPC CIDR 到 :80/:443(绝不 0.0.0.0/0)。
        for _p in (80, 443):
            alb.connections.allow_from(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.tcp(_p),
                "internal ALB: VPC CIDR only (no 0.0.0.0/0)",
            )
    else:
        # CloudFront origin-facing managed prefix list,按 region 映射(context 可覆盖)。
        # 之前只从 context 读,不传就降级 VPC-only → CloudFront 回源被 SG 拒 → /hub 504
        # (重建实撞:必须手动补 pl 才通)。给常用 region 内置默认值让一键部署即可用。
        # ap-southeast-1=pl-31a34658 已真机实测放行后 CloudFront→ALB 通;其余 region 值
        # 若未列,部署时传 -c cf_origin_facing_prefix_list=<pl-id>(否则降级 VPC-only)。
        _cf_pl = self.node.try_get_context(
            "cf_origin_facing_prefix_list"
        ) or _CF_PL_BY_REGION.get(self.region)
        if _cf_pl:
            listener.connections.allow_default_port_from(
                ec2.Peer.prefix_list(_cf_pl),
                "CloudFront origin-facing only (no 0.0.0.0/0)",
            )
        else:
            # 未知 region 且没传 context 则 fail-safe:只放 VPC 内(绝不退回 0.0.0.0/0)。
            listener.connections.allow_default_port_from(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                "fallback VPC-only: pass cf_origin_facing_prefix_list to lock to CloudFront",
            )
    # #187 P5 — hub-WS 数据面(HubTargetGroup + /hub/* listener rule + ASG 关联)
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

    # ╓─── [P2b · #187] 数据面两级路由 - Redis + EdgeASG + EdgeTG ─╖
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
        # #272 — redis.subnet_ids 非空时显式选路由表 Redis/Valkey 子网(imported
        # 私有子网场景);缺省回落 PRIVATE_WITH_EGRESS → private_subnets。
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
        # #281 复用现网:配了 existing_subnet_group_arn 就复用客户已有子网组
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
        # #271 引擎可选 redis|valkey。Valkey 是 Redis OSS 的 BSD fork,ElastiCache
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
        # 托管不显式指定(设计决策)。
        _redis_default_ver = "7.2" if _redis_engine == "valkey" else "7.1"
        _redis_engine_version = str(
            _redis_cfg.get("engine_version") or _redis_default_ver
        )
        # 参数组 family:引擎 + major(如 redis7 / valkey7)。DoD 要求显式建、
        # 不用 default parameter group(便于后续调 maxmemory-policy 等)。
        _redis_major = _redis_engine_version.split(".")[0]
        _redis_pg_family = f"{_redis_engine}{_redis_major}"
        # #281 复用现网:配了 existing_parameter_group_arn 就复用客户已有参数组
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
            # 显式关 transit 加密(设计决策):SG 隔离即够(私网内 6379 只对 host/edge
            # SG),不开 TLS/auth_token —— 拉长部署链、host-agent redis-py +
            # lua-resty-redis 都要额外配。显式 False 优于隐式(防未来引擎默认变化)。
            transit_encryption_enabled=False,
        )
        # #281 只在自建时加依赖;复用现网(existing_*_arn)时不 depend on 外部资源。
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
        # #187 P5 — hub_tg 已下线,EdgeTG 是数据面唯一 target group。此处保留
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
        # edge SG → host SG DNAT 端口段(host 数据面入站的唯一来源)。
        # 安全红线(design decision):host 的数据面流量只能来自 OpenResty
        # edge 集群,别处一律拒。端口段上下界从 config edge.dnat_port_{low,high} 读
        # (默认 [10000,15000] = 5001 槽);此处复用上方算好的 _edge_port_{low,high},
        # 与注入 host 位图的 DNAT_PORT_{LOW,HIGH} 同源(Property 2,防两边裂开)。
        # 关闭(stopped)租户保留路由不回收端口,累计租户多时按需在 config 抬高上界。
        sg.add_ingress_rule(
            ec2.Peer.security_group_id(_edge_sg.security_group_id),
            ec2.Port.tcp_range(_edge_port_low, _edge_port_high),
            "edge to host DNAT port range (the data-plane contract)",
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
        _edge_ud = ec2.UserData.for_linux()
        _edge_ud.add_commands(
            "set -euxo pipefail",
            # ENGINE_REDIS_ENDPOINT 需在 install-edge.sh 执行时可见(userdata
            # 属 CFN 模板,Redis endpoint token 在 deploy 期正确解析)。
            f'echo "ENGINE_REDIS_ENDPOINT={redis_endpoint}" >> /etc/environment',
            "mkdir -p /opt/openclaw-edge",
            f"for i in $(seq 1 60); do "
            f"aws s3 cp s3://{assets_bucket.bucket_name}/deployment/edge/ /opt/openclaw-edge/ --recursive --region {self.region} 2>/dev/null; "
            f"[ -f /opt/openclaw-edge/install-edge.sh ] && [ -f /opt/openclaw-edge/nginx.conf ] && break; "
            f'echo "waiting for edge assets in S3 ($i)"; sleep 10; done',
            '[ -f /opt/openclaw-edge/install-edge.sh ] || { echo "[edge-userdata] FATAL: edge assets missing after 600s" >&2; exit 1; }',
            f'ENGINE_REDIS_ENDPOINT="{redis_endpoint}" EDGE_LISTEN_PORT=8080 '
            "bash /opt/openclaw-edge/install-edge.sh",
        )
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
        # #272 — edge.subnet_ids 非空时显式选 OpenResty edge 子网;缺省回落
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
        _edge_asg.attach_to_application_target_group(edge_tg)

    # ========== CloudFront ==========
    # 1.3.4 (#61): support two-distribution mode for security boundary between
    # operator console and per-tenant dashboards. Configured via:
    #
    #   cloudfront:
    #     console_domain: "console.example.com"     # operator console (S3)
    #     console_cert_arn: "arn:aws:acm:us-east-1:..."
    #     app_domain:     "app.example.com"         # per-tenant dashboards (ALB)
    #     app_cert_arn:   "arn:aws:acm:us-east-1:..."
    #
    # If both pairs are set → DUAL mode: two distinct CloudFront distributions
    # with independent ACM certs. Cognito session cookie scoped to console_domain
    # only — tenant dashboards on app_domain physically cannot read it.
    # If unset (or only legacy custom_domain set) → LEGACY single-distribution
    # mode, kept for backward-compat with v1.3.3 and earlier deployments.
    # Explicit OAC with a region-suffixed name. The default auto-named OAC is
    # derived from the stack name, so a same-named stack in another region
    # (ap-southeast-1) collides on this account-global CloudFront resource.
    # #270 — cloudfront.enabled 开关(缺省 true=建,向后兼容)。false 时整个
    # CloudFront 构建段零资源(不建任何 Distribution/OAC/Function/ResponseHeaders),
    # console/dashboard host 回落 ALB DNS,cf_distribution=None,cf id 输出为空。
    # 内网/自管入口(internal ALB 或客户自带 CDN)时关掉,省 CF 资源。
    _cf_enabled = bool((CFG.get("cloudfront", {}) or {}).get("enabled", True))
    if _cf_enabled:
        _assets_oac = cloudfront.S3OriginAccessControl(
            self,
            "AssetsOAC",
            origin_access_control_name=f"openclaw-assets-oac{self._gsuffix}",
        )
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(
            assets_bucket, origin_access_control=_assets_oac
        )
        # CloudFront Function: rewrite /console/ → /console/index.html, / → /console/index.html
        url_rewrite_fn = cloudfront.Function(
            self,
            "UrlRewrite",
            function_name=f"openclaw-url-rewrite{self._gsuffix}",
            code=cloudfront.FunctionCode.from_inline("""
function handler(event) {
  var uri = event.request.uri;
  if (uri === '/') {
    return { statusCode: 302, statusDescription: 'Found',
      headers: { location: { value: '/console/' } } };
  }
  if (uri === '/console' || uri === '/console/') {
    event.request.uri = '/console/index.html';
  }
  return event.request;
}"""),
        )

        # Security response headers for edge-served content (#88 follow-up).
        # Static assets (S3 origin) shipped without HSTS / anti-clickjacking /
        # nosniff headers; add them at the edge so every cached response carries
        # them without touching each HTML file. Applied to the console/static
        # behaviors below.
        # #63 — CSP for XSS-in-depth. 前端 chat/console 内联 <script> 已全部
        # 搬到 js/*.js(console/chat/js/auth.js/chat.js 与 console/js/auth.js),
        # setup.sh 注入的账号占位符走 <script type=application/json>(不受 CSP
        # 执行策略约束)。不采用 'unsafe-inline'(对主威胁 renderMd/innerHTML
        # 注入防护为零),静态托管无服务端也不做 nonce(静态 nonce=常量=没用)。
        # 'unsafe-eval' 保留给 Alpine.js v3(x-data/@click 用 new Function 求值,
        # 拿掉整个 console 挂);marked/alpine CDN 白名单显式列。connect-src
        # 覆盖同源 /hub /chat/sign /tenants + Cognito /oauth2/token 跨域 fetch
        # (Cognito domain 由部署时决定,不硬编码进 CSP,故收敛成 https:/wss:)。
        _csp = (CFG.get("cloudfront", {}) or {}).get("csp") or (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            # #177 — agent 出图/附件走 hub 签发的 S3 直连预签名 URL(跨源),
            # 预签名 host 部署时才定、无法硬编码,收敛成 https:(同 connect-src)。
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' https: wss:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "object-src 'none'"
        )
        sec_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "SecHeadersPolicy",
            response_headers_policy_name=f"openclaw-sec-headers{self._gsuffix}",
            comment="OpenClaw edge security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=_csp,
                    override=True,
                ),
            ),
        )

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
            # ===== DUAL mode: two distributions, two certs, two aliases =====
            # Distribution A: console — S3 origin only, /console/* + redirect / → /console/
            console_cf = cloudfront.Distribution(
                self,
                "ConsoleCF",
                comment="OpenClaw Operator Console",
                domain_names=[console_domain],
                certificate=acm.Certificate.from_certificate_arn(
                    self, "ConsoleCert", console_cert_arn
                ),
                default_behavior=cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    response_headers_policy=sec_headers_policy,
                    function_associations=[
                        cloudfront.FunctionAssociation(
                            function=url_rewrite_fn,
                            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        )
                    ],
                ),
                default_root_object="",
            )
            # Distribution B: per-tenant dashboards — ALB origin only, /vm/*
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
            console_host = console_domain
            dashboard_host = app_domain
            console_cf_id = console_cf.distribution_id
            app_cf_id = app_cf.distribution_id
            cf_distribution = console_cf  # for downstream Cognito wiring
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
                    function_associations=[
                        cloudfront.FunctionAssociation(
                            function=url_rewrite_fn,
                            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        )
                    ],
                ),
                additional_behaviors={
                    "/console/*": cloudfront.BehaviorOptions(
                        origin=s3_origin,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                        response_headers_policy=sec_headers_policy,
                        function_associations=[
                            cloudfront.FunctionAssociation(
                                function=url_rewrite_fn,
                                event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                            )
                        ],
                    ),
                    # /chat/* — chat 小程序前端,与 console 同 S3 origin(桶根 chat/,
                    # 见 setup.sh 把 console/chat/index.html 传到 s3://<assets>/chat/)。
                    # 缺这条 behavior 时 /chat/* 走 default→ALB(metal)→404。不挂
                    # url_rewrite_fn(那是 /console 路径改写专用);chat 是单 index.html。
                    "/chat/*": cloudfront.BehaviorOptions(
                        origin=s3_origin,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                        response_headers_policy=sec_headers_policy,
                    ),
                    # #187 P5 — /hub/* behavior 已随 claw-hub 数据面下线一并删除。
                    # 数据面 WebSocket 走 /ws/{tenant_id} 直连 microVM gateway,由
                    # ALB default forward EdgeTG(P2b-iac)承担。
                },
                default_root_object="",
            )

            # Single mode: console and dashboard share the same host
            console_host = custom_domain or cf_distribution.distribution_domain_name
            dashboard_host = console_host
            console_cf_id = cf_distribution.distribution_id
            app_cf_id = cf_distribution.distribution_id
    else:
        # #270 gate 关闭:无 CloudFront。host 回落 ALB DNS(internal ALB 时
        # 是内网 DNS;客户自管 CDN 指到该 ALB)。cf_distribution=None → 下游
        # Cognito wiring 只在 custom_domain 真值时才 deref,故此处安全。
        cf_distribution = None
        console_host = alb.load_balancer_dns_name
        dashboard_host = alb.load_balancer_dns_name
        console_cf_id = ""
        app_cf_id = ""
        custom_domain = ""
        dual_mode = False

    # ========== Assets bucket CORS (chat mini-app 图片功能) ==========
    # The chat mini-app uploads/downloads images via S3 presigned URLs
    # directly from the browser; without CORS the browser blocks the PUT
    # ("Failed to fetch"). Scope AllowedOrigins to the real CloudFront host
    # (console_host, set by both single- and dual-domain branches) — NOT "*"
    # — to keep minimal exposure. Managed via CustomResource because the
    # bucket is RETAIN (inline cors on an existing bucket won't reliably
    # update). This codifies the CORS that was first applied by hand during
    # the 2026-06-27 image-feature bring-up.
    _media_cors_params = {
        "Bucket": assets_bucket.bucket_name,
        "CORSConfiguration": {
            "CORSRules": [
                {
                    "AllowedOrigins": [f"https://{console_host}"],
                    "AllowedMethods": ["GET", "PUT"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        },
    }
    cr.AwsCustomResource(
        self,
        "AssetsCors",
        install_latest_aws_sdk=False,
        on_create=cr.AwsSdkCall(
            service="S3",
            action="putBucketCors",
            parameters=_media_cors_params,
            physical_resource_id=cr.PhysicalResourceId.of("assets-cors"),
        ),
        on_update=cr.AwsSdkCall(
            service="S3",
            action="putBucketCors",
            parameters=_media_cors_params,
            physical_resource_id=cr.PhysicalResourceId.of("assets-cors"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_statements(
            [
                iam.PolicyStatement(
                    actions=["s3:PutBucketCORS"],
                    resources=[assets_bucket.bucket_arn],
                ),
            ]
        ),
    )

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
