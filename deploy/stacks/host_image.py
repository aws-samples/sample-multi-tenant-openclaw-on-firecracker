# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Host golden-AMI bake via EC2 Image Builder (#389 v2 block 2).

Not to be confused with ``stacks/image.py``, which bakes the *guest microVM rootfs*
through CodeBuild + debootstrap. This stack bakes the *host EC2 AMI*: an Ubuntu 24.04
image with provision-host.sh already applied, so a host boots with every component
already on disk.

Why: network installs at boot are unreliable. A wrong-arch awscli zip, a missing
aarch64 vmlinux suffix and a Firecracker tarball 404 have each already cost a host its
600s lifecycle hook, which puts the ASG into an ABANDON-and-replace loop that never
converges. Baking the downloads means the golden boot path performs none of them.

Shape, and why each piece is what it is:

  component      provision-host.sh inlined into an AWSTOE document, NOT fetched from S3.
                 A bake that downloads its own script would reintroduce exactly the
                 network dependency this stack exists to remove, and would let the baked
                 bytes drift from the LaunchTemplate-bound digest.
  recipe         parent image is the Canonical public SSM parameter for the configured
                 arch, so a re-bake picks up the current Ubuntu without a code edit.
  distribution   writes the output AMI id to an SSM parameter under /imagebuilder/, which
                 ha_edge.py's LaunchTemplate reads as ``resolve:ssm:``. Per the EC2 docs,
                 changing that parameter does NOT touch running instances — which is K1
                 (no automatic instance refresh) with no extra machinery.
  execution role custom, carrying EC2ImageBuilderExecutionPolicy. AWS explicitly
                 recommends against passing the service-linked role here; the SLR also
                 lacks ssm:PutParameter, so distribution would fail on it.
  pipeline       no schedule. A bake runs when a human or the API asks for one; an
                 unattended nightly re-bake would silently change what the next scale-out
                 boots.

This is a separate stack for the same reason stacks/image.py is: CloudFormation's
rollback boundary is one stack, and a failed bake must not roll back the control plane.
"""

import hashlib
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_imagebuilder as imagebuilder,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

from stacks._helpers import host_golden_ami_parameter_name

# Canonical's published AMI-id parameters. Public, so any account can read them; the
# execution role's managed policy already grants ssm:GetParameter on /aws/service/*.
_UBUNTU_SSM = {
    "arm64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id",
    "amd64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
}

# Image Builder component `data` 的【真实】上限:16000 字节(2026-08-05 真机 CFN 部署实测:
# "Model validation failed (#/Data: expected maxLength: 16000)")。此前误设 64KiB。脚本走
# S3Download(不内联),故文档本身远小于此;保留守卫防未来往文档里塞大内联块又踩线。
_COMPONENT_MAX_BYTES = 16000


def _component_document(
    provision_s3: str, provision_sha256: str,
    fluent_bit_s3: str, fluent_bit_sha256: str, recipe_version: str
) -> str:
    """An AWSTOE document that S3Downloads the two scripts, verifies their SHA256, then runs
    provision.

    为什么 S3Download 而不是内联 heredoc:两段脚本合计 ~19KB,超过 Image Builder component 的
    【真实 16000 字节 data 上限】(2026-08-05 真机部署实测:CFN 报 "expected maxLength: 16000")。
    内联放不下,故按 AWS 文档的 S3Download 逃生口把脚本作为 CDK 资产上传,bake 时下载。

    这【不】破坏 golden 启动路径的"零下载"承诺:下载发生在【烤制的一次性构建机】上(不是 host 开机
    路径),且 provision-host.sh 是烤进 AMI 的内容(与 LaunchTemplate 摘要绑定的是 init-host.sh,不是
    provision)。字节精确由 SHA256 校验保证:下载后 sha256sum -c,不符即 Abort,不会烤出跑了错内容的镜像。
    OC_PROVISION_BAKE=1 打开 provision 的 scrub-and-verify(镜像带 host 身份就 fail-bake)。
    """
    return f"""name: openclaw-host-provision
description: Installs the OpenClaw host components (#389 v2 block 2). Runs provision-host.sh with OC_PROVISION_BAKE=1, which scrubs and then verifies that no host identity remains in the image.
schemaVersion: 1.0
phases:
  - name: build
    steps:
      - name: DownloadFluentBitInstaller
        action: S3Download
        onFailure: Abort
        timeoutSeconds: 120
        inputs:
          - source: {fluent_bit_s3}
            destination: /opt/openclaw/install-fluent-bit.sh
      - name: DownloadProvisionScript
        action: S3Download
        onFailure: Abort
        timeoutSeconds: 120
        inputs:
          - source: {provision_s3}
            destination: /opt/openclaw/provision-host.sh
      - name: VerifyScriptDigests
        action: ExecuteBash
        onFailure: Abort
        timeoutSeconds: 60
        inputs:
          commands:
            - |-
              set -euo pipefail
              # 下载的字节必须逐字等于仓库脚本(S3 对象可能被换)——sha256sum -c fail 即 Abort,
              # 不会烤出跑了错 provision 的镜像。摘要在 deploy 期由 CDK 资产内容算出、焊进本文档。
              printf '%s  %s\\n' '{provision_sha256}' /opt/openclaw/provision-host.sh | sha256sum -c -
              printf '%s  %s\\n' '{fluent_bit_sha256}' /opt/openclaw/install-fluent-bit.sh | sha256sum -c -
              chmod 0755 /opt/openclaw/provision-host.sh /opt/openclaw/install-fluent-bit.sh
      - name: RunProvision
        action: ExecuteBash
        onFailure: Abort
        timeoutSeconds: 3600
        inputs:
          commands:
            - |-
              set -euo pipefail
              export OC_PROVISION_BAKE=1
              export OC_PROVISION_RECIPE_VERSION='{recipe_version}'
              export OC_PROVISION_FLUENT_BIT_INSTALLER=/opt/openclaw/install-fluent-bit.sh
              # bake 路径必须 fail-closed。烤镜像的整个理由是启动路径零下载,所以
              # 「S3 拿不到就回落 github」在这里是假绿:镜像照样烤出来、validate 照样过,
              # 而那台镜像其实请求过 github.com。构建实例有该前缀的 s3:GetObject(见本文件
              # build_role 的授权),所以这里取不到就是真出问题,该 Abort 而不是降级。
              export OC_FC_REQUIRE_S3=1
              bash /opt/openclaw/provision-host.sh
  - name: validate
    steps:
      - name: AssertZeroDownloadBootPath
        action: ExecuteBash
        onFailure: Abort
        timeoutSeconds: 300
        inputs:
          commands:
            - |-
              set -euo pipefail
              # The acceptance criterion for this whole block: a golden host must reach a
              # running state without fetching anything. Each missing component below is
              # one download the boot path would have to do, so assert them all here
              # rather than discover it on a host whose lifecycle hook is already ticking.
              fail=0
              for b in aws firecracker jailer; do
                command -v "$b" >/dev/null 2>&1 || {{ echo "MISSING binary: $b"; fail=1; }}
              done
              # Fluent Bit's official package installs off PATH, at /opt/fluent-bit/bin
              # (真机 2026-08-05). Probing PATH here reported it missing on an image that
              # had it, and the same wrong probe in the installer would have made every
              # golden boot reinstall it over the network. Assert the packaged location.
              for f in /opt/fluent-bit/bin/fluent-bit \\
                       /opt/openclaw/baked/vmlinux /etc/openclaw/.ami-provisioned; do
                [ -s "$f" ] || {{ echo "MISSING file: $f"; fail=1; }}
              done
              dpkg -s aws-otel-collector >/dev/null 2>&1 || {{ echo "MISSING pkg: aws-otel-collector"; fail=1; }}
              # The cross-tenant hazard. host_vm_key is per-host and its public half is
              # injected into every tenant microVM, so one baked key would let any host
              # SSH into any tenant's microVM on any host. provision's scrub already
              # checks this; re-checked here because validate runs after all build steps,
              # so it also catches a key created by a step added later.
              for leak in /etc/openclaw/host_vm_key /etc/openclaw/host_vm_key.pub /etc/platform.env; do
                [ ! -e "$leak" ] || {{ echo "LEAK: $leak present in image"; fail=1; }}
              done
              [ "$fail" = 0 ] || {{ echo "golden AMI validation failed"; exit 1; }}
              echo "golden AMI validated: components present, no host identity"
      - name: AssertProvisionIsIdempotent
        action: ExecuteBash
        onFailure: Abort
        timeoutSeconds: 900
        inputs:
          commands:
            - |-
              set -euo pipefail
              # A golden host runs configure only, but the plain-AMI path runs provision on
              # a machine that may already be provisioned, and a re-bake runs it twice.
              # Re-running here proves the idempotency claim on the real image instead of
              # in a comment. NOT in bake mode: the scrub already ran and passed, and
              # re-running it would only re-delete nothing.
              before="$(sha256sum /etc/openclaw/.ami-provisioned | cut -d' ' -f1)"
              OC_PROVISION_RECIPE_VERSION='{recipe_version}' \\
                OC_PROVISION_FLUENT_BIT_INSTALLER=/opt/openclaw/install-fluent-bit.sh \\
                bash /opt/openclaw/provision-host.sh
              after="$(sha256sum /etc/openclaw/.ami-provisioned | cut -d' ' -f1)"
              # provisioned_at is a timestamp, so the marker legitimately changes. What
              # must NOT change is the component set, which the previous step asserted and
              # which a re-run that reinstalled something would have broken.
              command -v firecracker >/dev/null && command -v aws >/dev/null
              [ -s /opt/openclaw/baked/vmlinux ]
              echo "provision re-run clean (marker $before -> $after)"
"""


class OpenClawHostImageStack(cdk.Stack):
    """EC2 Image Builder pipeline producing the host golden AMI."""

    def __init__(self, scope: Construct, id: str, *, cfg: dict, gsuffix: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        host_cfg = cfg.get("host", {}) or {}
        golden_cfg = host_cfg.get("golden_ami", {}) or {}
        if not golden_cfg.get("build_pipeline", False):
            # Off by default. An account that never bakes should carry no Image Builder
            # resources at all, and the ASG keeps using the plain Canonical AMI.
            return

        recipe_version = str(golden_cfg.get("recipe_version", "1.0.0"))
        arch = "arm64" if host_cfg.get("arch", "x86_64") == "arm64" else "amd64"
        # Bake on a small instance of the SAME arch as the target. Cross-arch is not a
        # choice here: the image built on x86 cannot boot a Graviton host.
        build_instance = golden_cfg.get("build_instance_type") or (
            "c7g.large" if arch == "arm64" else "c7i.large"
        )

        userdata_dir = Path(__file__).parent.parent / "userdata"
        provision_path = userdata_dir / "provision-host.sh"
        fluent_bit_path = (
            Path(__file__).parent.parent / "edge" / "fluent-bit" / "install-fluent-bit.sh"
        )
        # 两段脚本合计 ~19KB > component 的 16000B data 上限,故作为 CDK 资产上传、bake 时 S3Download。
        # 摘要在 deploy 期按【文件内容】算,焊进 component 文档;bake 时 sha256sum -c 校验下载字节。
        _sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
        provision_asset = s3_assets.Asset(self, "ProvisionScriptAsset", path=str(provision_path))
        fluent_bit_asset = s3_assets.Asset(self, "FluentBitInstallerAsset", path=str(fluent_bit_path))
        provision_s3 = f"s3://{provision_asset.s3_bucket_name}/{provision_asset.s3_object_key}"
        fluent_bit_s3 = f"s3://{fluent_bit_asset.s3_bucket_name}/{fluent_bit_asset.s3_object_key}"

        document = _component_document(
            provision_s3, _sha(provision_path),
            fluent_bit_s3, _sha(fluent_bit_path), recipe_version,
        )
        if len(document.encode()) > _COMPONENT_MAX_BYTES:
            raise ValueError(
                f"AWSTOE component document is {len(document.encode())}B; the Image "
                f"Builder limit is {_COMPONENT_MAX_BYTES}B."
            )

        component = imagebuilder.CfnComponent(
            self,
            "HostProvisionComponent",
            name=f"openclaw-host-provision{gsuffix}",
            platform="Linux",
            version=recipe_version,
            description="Install OpenClaw host components and verify a zero-download boot path",
            supported_os_versions=["Ubuntu 24"],
            data=document,
        )

        recipe = imagebuilder.CfnImageRecipe(
            self,
            "HostImageRecipe",
            name=f"openclaw-host{gsuffix}",
            version=recipe_version,
            # `ssm:` prefix is required when the parent image is set through anything
            # other than the console (per the Image Builder docs); without it the string
            # is read as a literal AMI id and the recipe create fails.
            parent_image=f"ssm:{_UBUNTU_SSM[arch]}",
            components=[
                imagebuilder.CfnImageRecipe.ComponentConfigurationProperty(
                    component_arn=component.attr_arn
                )
            ],
            # Keep the SSM agent in the output image. Image Builder's post-build cleanup
            # uninstalls it when it installed it, and Ubuntu's Canonical AMI ships it via
            # snap — so leaving this to the default risks an AMI with no agent. The host
            # runtime needs it: the control plane drives hosts through SSM (lifecycle
            # batches, host-agent commands), and an agentless host is unreachable.
            additional_instance_configuration=imagebuilder.CfnImageRecipe.AdditionalInstanceConfigurationProperty(
                systems_manager_agent=imagebuilder.CfnImageRecipe.SystemsManagerAgentProperty(
                    uninstall_after_build=False
                )
            ),
            block_device_mappings=[
                imagebuilder.CfnImageRecipe.InstanceBlockDeviceMappingProperty(
                    device_name="/dev/sda1",
                    ebs=imagebuilder.CfnImageRecipe.EbsInstanceBlockDeviceSpecificationProperty(
                        # Must be >= the root_volume_gb the LaunchTemplate asks for: EBS
                        # can grow a volume from a snapshot but never shrink it, so a
                        # smaller bake volume makes every host launch fail.
                        volume_size=int(host_cfg.get("root_volume_gb", 20)),
                        volume_type="gp3",
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                )
            ],
        )

        # ── Build-instance profile ────────────────────────────────────────────────────
        # The instance that runs the component. Needs to read its own component and, via
        # SSM agent, be driven by Image Builder. It does NOT need the host's runtime
        # permissions (DynamoDB, the assets bucket at large, …) — granting them would put
        # control-plane access on a throwaway build box.
        #
        # 一处【有意的例外】,并把上面那句「provision touches none of them」改准:
        # provision 现在要从 assets 桶读一个对象(Firecracker/jailer 的 tgz),因为取源从
        # github.com 的 releases 改成了自家 S3(10W 规模并发启 host 会撞 GitHub rate limit)。
        # 所以 bake 实例也需要读那一个前缀,否则 bake 会 AccessDenied → 回落 github,
        # 而 golden AMI 的验收第 2 条恰恰要求「bake 过程零 github.com 请求」。
        # 授权按最小面给,不违背上面那条原则:
        #   · 只 s3:GetObject,不给 ListBucket(aws s3 cp 单个 key 不需要它);
        #   · 账号钉死为本栈账号,区域后缀用 * 兼容 openclaw-assets-<acct>[-<region>];
        #   · 前缀锁死在 deployment/binaries/firecracker/,拿不到脚本、镜像清单或任何
        #     控制面对象。
        # 这与「不给 throwaway build box 控制面访问权」是一致的 —— 它拿到的是一个公开发行版
        # 二进制的自家副本,不是控制面数据。
        build_role = iam.Role(
            self,
            "HostImageBuildRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="EC2 Image Builder build instance for the OpenClaw host AMI",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "EC2InstanceProfileForImageBuilder"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )
        # S3Download 用构建实例角色读两个脚本资产(仅这两个对象;不给宽泛 S3)。
        provision_asset.grant_read(build_role)
        fluent_bit_asset.grant_read(build_role)
        # bake 期读 Firecracker/jailer 的自家副本(理由与边界见上方注释)。
        build_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::openclaw-assets-{self.account}*"
                    "/deployment/binaries/firecracker/*"
                ],
            )
        )
        build_profile = iam.InstanceProfile(
            self, "HostImageBuildProfile", role=build_role
        )

        # ── Execution role ────────────────────────────────────────────────────────────
        # What Image Builder itself assumes. Custom rather than the service-linked role
        # on AWS's own recommendation, and out of necessity: the SLR has no
        # ssm:PutParameter, so SSM distribution fails on it.
        execution_role = iam.Role(
            self,
            "HostImageExecutionRole",
            assumed_by=iam.ServicePrincipal("imagebuilder.amazonaws.com"),
            description="EC2 Image Builder execution role for the OpenClaw host AMI",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "EC2ImageBuilderExecutionPolicy"
                )
            ],
        )
        param_name = host_golden_ami_parameter_name(gsuffix)
        # EC2ImageBuilderExecutionPolicy already covers ssm:PutParameter on
        # /imagebuilder/* and ec2:DescribeImages. Scoped again to this one parameter, so
        # the intent is readable at the resource and survives a managed-policy change.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter", "ssm:GetParameter"],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=param_name.lstrip("/"),
                    )
                ],
            )
        )
        # data_type aws:ec2:image makes SSM validate the value is a real AMI id, which
        # needs DescribeImages. A bad value here would otherwise only surface as every
        # host in the ASG failing to launch.
        execution_role.add_to_policy(
            iam.PolicyStatement(actions=["ec2:DescribeImages"], resources=["*"])
        )

        infra = imagebuilder.CfnInfrastructureConfiguration(
            self,
            "HostImageInfra",
            name=f"openclaw-host-image{gsuffix}",
            instance_profile_name=build_profile.instance_profile_name,
            instance_types=[build_instance],
            # Keep the box on failure so a failed bake can be inspected. It is a
            # throwaway instance with no tenant data and no control-plane permissions,
            # and a bake failure whose cause is already terminated costs another cycle.
            terminate_instance_on_failure=False,
            instance_metadata_options=imagebuilder.CfnInfrastructureConfiguration.InstanceMetadataOptionsProperty(
                http_tokens="required",  # IMDSv2 only
                http_put_response_hop_limit=1,
            ),
        )

        distribution = imagebuilder.CfnDistributionConfiguration(
            self,
            "HostImageDistribution",
            name=f"openclaw-host-image{gsuffix}",
            distributions=[
                imagebuilder.CfnDistributionConfiguration.DistributionProperty(
                    region=self.region,
                    ami_distribution_configuration={
                        # {{ imagebuilder:buildDate }} is substituted by the service, so
                        # each bake produces a distinctly named AMI and an operator can
                        # tell which image a host booted.
                        "name": f"openclaw-host-{recipe_version}-{{{{ imagebuilder:buildDate }}}}",
                        "description": "OpenClaw host golden AMI (#389 v2): components pre-installed, no host identity",
                        "amiTags": {
                            "Project": "openclaw",
                            "Role": "metal-host",
                            "RecipeVersion": recipe_version,
                        },
                    },
                    ssm_parameter_configurations=[
                        imagebuilder.CfnDistributionConfiguration.SsmParameterConfigurationProperty(
                            parameter_name=param_name,
                            data_type="aws:ec2:image",
                        )
                    ],
                )
            ],
        )

        pipeline = imagebuilder.CfnImagePipeline(
            self,
            "HostImagePipeline",
            name=f"openclaw-host{gsuffix}",
            description="Bake the OpenClaw host golden AMI",
            image_recipe_arn=recipe.attr_arn,
            infrastructure_configuration_arn=infra.attr_arn,
            distribution_configuration_arn=distribution.attr_arn,
            execution_role=execution_role.role_arn,
            status="ENABLED",
            # Off, and it must stay off. Enhanced metadata makes Image Builder create its own
            # Systems Manager inventory association on the build instance, but SSM allows only
            # ONE inventory association per managed instance. Any account that already has an
            # org-wide inventory association therefore fails the build. That is exactly what
            # happened here (真机 2026-08-05, us-west-2): the account's
            # PVRE-PvreInventoryCollectionDocument association targets `tag:Patch Group=DEV`,
            # a tag the account applies to the build instance, so the agent reported
            # "aws:softwareInventory detected multiple inventory configurations associated with
            # one instance … Conflicting inventory configuration IDs", the InventoryCollection
            # step FAILED with onFailure: Abort, and no AMI was produced — after every component
            # and both golden-AMI assertions had already passed. AWS documents this scenario and
            # gives turning the flag off as the resolution. It only collects OS/package metadata
            # for reporting; the build, the validate-phase assertions and the sanitize step are
            # all independent of it (see the managed build-image workflow's step conditions).
            enhanced_image_metadata_enabled=False,
            # The component's validate phase is where the zero-download and no-identity
            # assertions live; that runs as part of the build regardless. image_tests is
            # the separate post-AMI test launch — enabled so a fresh boot of the produced
            # AMI is proven before it is distributed.
            image_tests_configuration=imagebuilder.CfnImagePipeline.ImageTestsConfigurationProperty(
                image_tests_enabled=True,
                timeout_minutes=90,
            ),
            # No `schedule`: a bake happens when asked. See the module docstring (K1).
        )

        cdk.CfnOutput(
            self,
            "HostGoldenAmiParameter",
            value=param_name,
            description="SSM parameter holding the host golden AMI id (LaunchTemplate reads it via resolve:ssm)",
        )
        cdk.CfnOutput(
            self,
            "HostImagePipelineArn",
            value=pipeline.attr_arn,
            description="Start a bake with: aws imagebuilder start-image-pipeline-execution --image-pipeline-arn <this>",
        )
