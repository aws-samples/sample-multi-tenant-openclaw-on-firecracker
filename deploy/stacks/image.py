# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_s3 as s3,
    aws_codebuild as codebuild,
    aws_s3_assets as s3_assets,
    custom_resources as cr,
    Duration,
)
from constructs import Construct
from pathlib import Path


class OpenClawImageStack(cdk.Stack):
    """Golden-image bake, split out of the orchestrator stack.

    Why a separate stack: the bake runs a CodeBuild project (docker-in-docker
    debootstrap, ~10-40min). When it lived inside the main stack behind a
    BLOCKING custom resource, a build failure failed that resource and rolled
    back the WHOLE orchestrator stack. CloudFormation's rollback boundary is a
    single stack, so isolating the bake here means a bad build only touches
    this stack — the control plane / data plane stay untouched.

    Non-blocking: the custom resource fires start-build and returns immediately.
    Deploy success no longer waits on (nor depends on) the build outcome. The
    ASG in the main stack no longer depends on image readiness either; on a
    brand-new region the first host may boot before the image is baked and churn
    a few minutes (lifecycle-hook timeout, NOT a deploy failure) until the bake
    lands the rootfs in S3. Re-bake by bumping image.version (custom-resource
    property diff) or `cdk deploy OpenClawImage` standalone.

    CodeBuild runs on the managed network (needs internet egress to pull
    packages + push to S3) — no VPC config, nothing to misconfigure.
    """

    def __init__(self, scope: Construct, id: str, *, cfg: dict, gsuffix: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        img_cfg = cfg.get("image", {}) or {}
        image_version = str(img_cfg.get("version", "v1.0"))
        if not img_cfg.get("build_in_stack", True):
            return  # bake disabled: reuse existing image or bake out-of-band

        # Assets bucket is created by the orchestrator stack. Reference it by its
        # deterministic name (openclaw-assets-<account><suffix>) so this stack
        # only reads a string — no CFN export that would lock the main stack.
        assets_bucket = s3.Bucket.from_bucket_name(
            self, "AssetsBucket", f"openclaw-assets-{self.account}{gsuffix}"
        )

        # Repo source as an S3 asset (CDK zips + uploads). Exclude heavy/local-only
        # dirs so the upload stays small and never carries secrets.
        repo_asset = s3_assets.Asset(
            self,
            "GoldenImageSource",
            path=str(Path(__file__).parent.parent.parent),
            exclude=[
                ".git",
                ".venv",
                "cdk.out",
                "node_modules",
                "**/node_modules",
                "*.bak",
                "*.bak-*",
                ".localbin",
                ".remote-drift",
                "engineering",
                "docs/**",
                "presentations/**",
                "*.pyc",
                "**/__pycache__",
                ".ruff_cache",
            ],
        )

        # CodeBuild service role: read the source asset, read/write the assets
        # bucket (push baked image), and write its own logs.
        cb_role = iam.Role(
            self,
            "GoldenImageBuildRole",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
        )
        assets_bucket.grant_read_write(cb_role)
        repo_asset.grant_read(cb_role)
        cb_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        golden_project = codebuild.Project(
            self,
            "GoldenImageBuilder",
            project_name=f"openclaw-golden-image-builder{gsuffix}",
            role=cb_role,
            source=codebuild.Source.s3(
                bucket=repo_asset.bucket,
                path=repo_asset.s3_object_key,
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,  # docker-in-docker for debootstrap
            ),
            environment_variables={
                "ASSETS_BUCKET": codebuild.BuildEnvironmentVariable(
                    value=assets_bucket.bucket_name
                ),
                "IMAGE_VERSION": codebuild.BuildEnvironmentVariable(
                    value=image_version
                ),
                "AWS_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
            },
            build_spec=codebuild.BuildSpec.from_source_filename(
                "deploy/codebuild/buildspec-golden-image.yml"
            ),
            timeout=Duration.minutes(40),
        )

        # Non-blocking trigger: onEvent fires start-build and returns. There is
        # NO isComplete waiter — the deploy does not wait for, nor fail on, the
        # build outcome. A build failure surfaces in CodeBuild history, never in
        # a CloudFormation rollback. Re-fires on Create and whenever ImageVersion
        # changes (Update); a plain no-version-change redeploy is a no-op.
        cb_start_fn = _lambda.Function(
            self,
            "GoldenBuildStart",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.on_event",
            timeout=Duration.minutes(2),
            code=_lambda.Code.from_inline(
                "import boto3\n"
                "cb = boto3.client('codebuild')\n"
                "def on_event(event, ctx):\n"
                "    rt = event['RequestType']\n"
                "    if rt == 'Delete':\n"
                "        return {'PhysicalResourceId': event.get('PhysicalResourceId','golden-build')}\n"
                "    proj = event['ResourceProperties']['ProjectName']\n"
                "    b = cb.start_build(projectName=proj)['build']\n"
                "    return {'PhysicalResourceId': b['id'], 'Data': {'BuildId': b['id']}}\n"
            ),
        )
        cb_start_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["codebuild:StartBuild"],
                resources=[golden_project.project_arn],
            )
        )
        golden_provider = cr.Provider(
            self,
            "GoldenBuildProvider",
            on_event_handler=cb_start_fn,
        )
        image_ready = cdk.CustomResource(
            self,
            "GoldenImageReady",
            service_token=golden_provider.service_token,
            properties={
                "ProjectName": golden_project.project_name,
                # change forces re-bake when the image version changes
                "ImageVersion": image_version,
            },
        )
        image_ready.node.add_dependency(golden_project)
