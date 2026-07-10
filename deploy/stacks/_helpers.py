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
    - self_managed: 自建 /20,PUBLIC×3 + PRIVATE_WITH_EGRESS×3 + 3 NAT GW。
    - imported: 客户传 vpc_id + 3 public + 3 private,缺项 raise(fail-loud)。

    切档=改部署代码→重建栈(铁律 #3)。half-config 是隐性错的高发点,
    imported 半配一律 ValueError(不做"部分放行/降级",踩过 too many times)。
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
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
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
        if not (vpc_id and len(pubs) == 3 and len(privs) == 3):
            raise ValueError(
                "network.mode=imported requires non-empty vpc_id + exactly 3 "
                "public_subnet_ids + 3 private_subnet_ids (缺一 fail-loud)"
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
        return ec2.Vpc.from_vpc_attributes(
            scope,
            "Vpc",
            vpc_id=vpc_id,
            availability_zones=_stack_azs,
            public_subnet_ids=pubs,
            private_subnet_ids=privs,
            vpc_cidr_block=_imp_cidr,
        )
    raise ValueError(
        f"network.mode must be 'default_vpc' | 'self_managed' | 'imported', got {mode!r}"
    )
