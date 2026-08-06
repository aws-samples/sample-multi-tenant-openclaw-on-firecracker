"""Helper functions for CDK stack (shared across domain modules, issue #87)."""
import platform as _platform
from pathlib import Path
from aws_cdk import aws_ec2 as ec2


def _sam_build_image_for_host():
    """SAM build image tag for the deploy host's arch (avoids QEMU). pip still
    cross-downloads the aarch64 wheel to match the ARM_64 Lambda."""
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "public.ecr.aws/sam/build-python3.12:latest-arm64"
    return "public.ecr.aws/sam/build-python3.12:latest-x86_64"


def host_golden_ami_parameter_name(gsuffix):
    """SSM parameter holding the current host golden AMI id (#389 v2 block 2).

    Lives here, not in host_image.py, because two stacks must agree on it: the Image
    Builder pipeline WRITES it at distribution and the host LaunchTemplate READS it as
    ``resolve:ssm:``. A name computed independently on each side would drift into an ASG
    whose every launch fails on a nonexistent parameter.

    Under ``/imagebuilder/`` deliberately: the EC2ImageBuilderExecutionPolicy managed
    policy grants ``ssm:PutParameter`` only on that prefix, so any other name needs a
    hand-written policy and fails the bake at distribution time rather than at synth.
    """
    return f"/imagebuilder/openclaw/host-ami{gsuffix or ''}"


def _read_pyproject_version():
    """Best-effort read of the project version so the API can advertise it
    via /system/info. Falls back to "dev" if pyproject.toml is unreadable
    (e.g. during a test that mocks the filesystem)."""
    try:
        import re

        text = (Path(__file__).parent.parent.parent / "pyproject.toml").read_text()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return m.group(1) if m else "dev"
    except Exception:
        return "dev"


def _build_vpc(scope, net_cfg):
    """P2b · #187 FR-10 · INTERFACE-CONTRACT §6:三档 VPC。

    - default_vpc: 存量 from_lookup 默认 VPC(host 裸公网,不推荐)。
    - self_managed: 自建 /20,PUBLIC×3(/24)+ PRIVATE_ISOLATED×3(Database /26,
      给 Redis/ElastiCache 独占、无 NAT 出网)+ PRIVATE_WITH_EGRESS×3(/22)+ 3 NAT GW。
    - imported: 客户传 vpc_id + 3 public + 3 private,可选 3 database(Redis 独占);
      缺 database 则 Redis 回落私有子网(向后兼容);其余缺项 raise(fail-loud)。

    切档=改部署代码→重建栈(铁律 #3)。half-config 是隐性错的高发点,
    imported 半配一律 ValueError(不做"部分放行/降级",踩过 too many times)。

    #196 — 数据面与数据库子网分离:数据库层(ElastiCache Redis,未来 RDS)与数据面层
    (host/edge/microVM)子网物理隔离,是 AWS 数据面标准分层(DB 子网无 NAT 出网面,
    爆炸半径更小)。CIDR 向后兼容:Database 用 /26 且排在 public 之后、private 之前,
    恰好填进 public(占 10.x.0-2.0/24)与 private(占 10.x.4/8/12.0/22)之间原本闲置
    的 10.x.3.0/24 缝隙 —— 存量 public/private 子网 CIDR 保持 byte-identical,仍是
    /20 不用扩网。
    """
    mode = (net_cfg or {}).get("mode", "default_vpc")
    if mode == "default_vpc":
        return ec2.Vpc.from_lookup(scope, "Vpc", is_default=True)
    if mode == "self_managed":
        sm = net_cfg.get("self_managed") or {}
        cidr = sm.get("cidr") or "10.20.0.0/20"
        return ec2.Vpc(
            scope,
            "Vpc",
            vpc_name="openclaw-vpc",
            ip_addresses=ec2.IpAddresses.cidr(cidr),
            max_azs=3,
            nat_gateways=3,
            # 顺序即 CIDR 分配顺序:Public(/24)→ Database(/26,填 .3.0/24 缝)→
            # Private(/22)。Database 排 private 之前,否则 /26 撞进已切走的 /22 块
            # 报 SubnetCountExceedsRemainingSpace(实测)。见 docstring #196。
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                # Database:Redis/ElastiCache 独占的隔离层,无 NAT(数据库不需出网)。
                ec2.SubnetConfiguration(
                    name="Database",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=26,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=22,
                ),
            ],
        )
    if mode == "imported":
        imp = net_cfg.get("imported") or {}
        vpc_id = (imp.get("vpc_id") or "").strip()
        pubs = list(imp.get("public_subnet_ids") or [])
        privs = list(imp.get("private_subnet_ids") or [])
        dbs = list(imp.get("database_subnet_ids") or [])
        if not (vpc_id and len(pubs) == 3 and len(privs) == 3):
            raise ValueError(
                "network.mode=imported requires non-empty vpc_id + exactly 3 "
                "public_subnet_ids + 3 private_subnet_ids (缺一 fail-loud)"
            )
        # database_subnet_ids 可选(#196):传就 Redis 独占,不传则回落私有子网(存量
        # 兼容)。传了就必须是恰好 3 个(跨 3 AZ),半配 fail-loud。
        if dbs and len(dbs) != 3:
            raise ValueError(
                "network.mode=imported: database_subnet_ids 要么留空(Redis 回落私有"
                f"子网),要么恰好 3 个跨 AZ,当前 {len(dbs)} 个(半配 fail-loud)"
            )
        # from_vpc_attributes 要求 AZ 数量与 subnet id 数量对齐(CDK 靠 index 一一
        # 对应),用 stack.availability_zones 前 3 个(scope 是 stack)。跨栈 3 AZ
        # 部署也覆盖 —— 客户传 subnet 时按 AZ 顺序传即可。
        # vpc_cidr_block 必传:现有代码在 SG rule/route 里引用 `vpc.vpc_cidr_block`,
        # 未传会 CannotPerformOperationVpcCidr 崩。客户 imported 时须一起传自家 VPC CIDR。
        _stack_azs = list(scope.availability_zones)[:3]
        _imp_cidr = (imp.get("cidr") or "").strip()
        if not _imp_cidr:
            raise ValueError(
                "network.mode=imported requires imported.cidr (VPC CIDR block, "
                "used by SG rules referencing vpc.vpc_cidr_block)"
            )
        # #196 — 传了 database_subnet_ids 就登记为 isolated 子网(Redis 独占查询
        # subnet_type=PRIVATE_ISOLATED 命中);没传则不登记,ha_edge 的 Redis
        # 选子网回落 PRIVATE_WITH_EGRESS(存量行为不变)。
        _attrs = dict(
            vpc_id=vpc_id,
            availability_zones=_stack_azs,
            public_subnet_ids=pubs,
            private_subnet_ids=privs,
            vpc_cidr_block=_imp_cidr,
        )
        if dbs:
            _attrs["isolated_subnet_ids"] = dbs
        return ec2.Vpc.from_vpc_attributes(scope, "Vpc", **_attrs)
    raise ValueError(
        f"network.mode must be 'default_vpc' | 'self_managed' | 'imported', got {mode!r}"
    )
