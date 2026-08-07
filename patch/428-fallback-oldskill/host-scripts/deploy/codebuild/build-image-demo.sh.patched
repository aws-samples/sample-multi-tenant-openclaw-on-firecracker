#!/bin/bash
# build-image-demo.sh — 独立、最小、可单跑的 OpenClaw 镜像构建 pipeline demo(issue #148)。
#
# 定位:给人快速看懂"源码 → 构建 → golden image 到 S3"这一段链路,与主 CDK 控制面栈
# 完全解耦(不 cdk deploy 整栈也能跑)。仅演示/教学,不替换生产 in-stack bake
# (生产仍走 stack.py 的 GoldenImageBuilder + 同一份 buildspec)。
#
# 复用现成零件,不另起构建逻辑:
#   build-rootfs.sh  +  buildspec-golden-image.yml  +  bake-in-container.sh
# demo 只多做三件解耦的事:① 建独立 demo 桶(开 versioning)② 建只授权该桶的独立
# CodeBuild role ③ 参数化 CPU 架构(arm64 / x86_64),证明同一套脚本两种架构都能烤。
#
# 用法:
#   ./build-image-demo.sh <PROFILE> <REGION> [--arch arm64|x86_64] [--version vX] [--sample NAME]
#   ./build-image-demo.sh ${AWS_PROFILE} us-east-1 --arch arm64   --version demo-arm
#   ./build-image-demo.sh ${AWS_PROFILE} us-east-1 --arch x86_64  --version demo-x86
#
# 边界:demo 用独立空桶(openclaw-imgdemo-<acct>-<region>),在其中写自己的
# manifest.json 演示"版本指针",不碰任何生产桶,生产 host 不会误拉 demo 镜像。
set -euo pipefail

PROFILE="${1:?用法: $0 <PROFILE> <REGION> [--arch ...] [--version ...] [--sample ...]}"
REGION="${2:?REGION (如 us-east-1)}"
shift 2

ARCH="arm64"; VERSION=""; SAMPLE="finance-agent"
while [ $# -gt 0 ]; do
  case "$1" in
    --arch)    ARCH="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --sample)  SAMPLE="$2"; shift 2 ;;
    *) echo "❌ 未知参数: $1"; exit 1 ;;
  esac
done

# 架构 → CodeBuild 环境。arm64 用 ARM 容器,x86_64 用 LINUX 容器;两者都开
# privilegedMode(docker-in-docker 跑 debootstrap 需要)。buildspec 和容器脚本
# 是架构无关的(docker pull ubuntu:22.04 按宿主架构自动选多架构 manifest 层)。
case "$ARCH" in
  arm64|aarch64)
    ARCH=arm64; CB_TYPE="ARM_CONTAINER"; CB_IMAGE="aws/codebuild/amazonlinux2-aarch64-standard:3.0" ;;
  x86_64|amd64)
    ARCH=x86_64; CB_TYPE="LINUX_CONTAINER"; CB_IMAGE="aws/codebuild/amazonlinux2-x86_64-standard:5.0" ;;
  *) echo "❌ arch 只支持 arm64 或 x86_64,收到: $ARCH"; exit 1 ;;
esac
[ -n "$VERSION" ] || VERSION="demo-${ARCH}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
AWS="aws --profile $PROFILE --region $REGION"
ACCT=$($AWS sts get-caller-identity --query Account --output text)
BUCKET="openclaw-imgdemo-${ACCT}-${REGION}"
PROJ="openclaw-image-demo-${ARCH}"
ROLE_NAME="openclaw-image-demo-role"

echo "=== demo 参数 ==="
echo "  账号=$ACCT region=$REGION 架构=$ARCH 版本=$VERSION 样本=$SAMPLE"
echo "  CodeBuild=$CB_TYPE / $CB_IMAGE"
echo "  独立 demo 桶=$BUCKET(与生产 assets 桶解耦)"

echo "=== 1. 建独立 demo 桶 + 开 versioning(演示 S3 版本管理)==="
if ! $AWS s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    $AWS s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    $AWS s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
fi
$AWS s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
$AWS s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "  versioning=Enabled, public-access=blocked"

echo "=== 2. 打包源码(只烤镜像需要的,排除凭据/大目录)→ S3 source ==="
SRC_ZIP="/tmp/openclaw-imgdemo-src-${VERSION}.zip"
rm -f "$SRC_ZIP"
# config.yml 被 gitignore(靠 .example 持久化),干净 checkout 里可能没有。
# build-rootfs.sh:885 要读它拿 data_disk_mb。缺则从 .example 生成默认值,
# 让 demo 在任何干净 checkout 都能自足跑通(同 buildspec 对 openclaw.json 的处理)。
[ -f "$REPO_ROOT/config.yml" ] || cp "$REPO_ROOT/config.yml.example" "$REPO_ROOT/config.yml"
# deploy/userdata 必须打包:build-rootfs.sh:878 从这里 cp overlay-init 进 rootfs。
# 与生产 build-golden-on-codebuild.sh 的 allowlist 对齐(它也打 deploy/userdata)。
( cd "$REPO_ROOT" && zip -rq "$SRC_ZIP" \
    build-rootfs.sh "samples/${SAMPLE}" templates config.yml config.yml.example \
    deploy/codebuild deploy/userdata scripts/lib \
    -x '*/node_modules/*' '*/.git/*' '*.bak*' '*.bak-*' '*/__pycache__/*' \
       '.localbin/*' '*/.localbin/*' '.remote-drift/*' '*/.remote-drift/*' \
       '*.pem' '*.key' '*tenants-resp*' '*cred*' '*secret*' )
# fail-closed:zip 里混进凭据就中止
if unzip -l "$SRC_ZIP" | grep -iE 'tenants-resp|\.pem|gateway_token|localbin|remote-drift'; then
  echo "❌ source zip 含疑似凭据,中止"; exit 1
fi
$AWS s3 cp "$SRC_ZIP" "s3://${BUCKET}/codebuild-src/repo-${VERSION}.zip" >/dev/null
echo "  源已上传 s3://${BUCKET}/codebuild-src/repo-${VERSION}.zip ($(du -h "$SRC_ZIP" | cut -f1))"

echo "=== 3. 建/更新独立 CodeBuild role(只授权本 demo 桶,不碰生产)==="
if ! $AWS iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  $AWS iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  sleep 8
fi
# 每次刷新 policy(桶/region 可能变)。范围锁死本 demo 桶 + CloudWatch logs。
$AWS iam put-role-policy --role-name "$ROLE_NAME" --policy-name imgdemo-build --policy-document "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::${BUCKET}\",\"arn:aws:s3:::${BUCKET}/*\"]},
    {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"}
  ]}"
ROLE_ARN="arn:aws:iam::${ACCT}:role/${ROLE_NAME}"

echo "=== 4. 建/更新 CodeBuild 项目($CB_TYPE, privileged, LARGE)==="
# demo 写自己独立桶的 manifest.json(演示"版本指针"怎么工作),不碰生产桶,
# 所以不设 SKIP_MANIFEST。SAMPLE 透传给 build-rootfs.sh 选样本目录。
PROJ_CFG="{
  \"name\":\"${PROJ}\",
  \"source\":{\"type\":\"S3\",\"location\":\"${BUCKET}/codebuild-src/repo-${VERSION}.zip\",\"buildspec\":\"deploy/codebuild/buildspec-golden-image.yml\"},
  \"artifacts\":{\"type\":\"NO_ARTIFACTS\"},
  \"environment\":{
    \"type\":\"${CB_TYPE}\",
    \"image\":\"${CB_IMAGE}\",
    \"computeType\":\"BUILD_GENERAL1_LARGE\",
    \"privilegedMode\":true,
    \"environmentVariables\":[
      {\"name\":\"ASSETS_BUCKET\",\"value\":\"${BUCKET}\"},
      {\"name\":\"IMAGE_VERSION\",\"value\":\"${VERSION}\"},
      {\"name\":\"AWS_REGION\",\"value\":\"${REGION}\"},
      {\"name\":\"SAMPLE\",\"value\":\"${SAMPLE}\"}
    ]
  },
  \"serviceRole\":\"${ROLE_ARN}\"
}"
echo "$PROJ_CFG" > /tmp/imgdemo-proj.json
if $AWS codebuild batch-get-projects --names "$PROJ" --query "projects[0].name" --output text 2>/dev/null | grep -q "$PROJ"; then
  $AWS codebuild update-project --cli-input-json file:///tmp/imgdemo-proj.json >/dev/null
else
  $AWS codebuild create-project --cli-input-json file:///tmp/imgdemo-proj.json >/dev/null
fi

echo "=== 5. 触发 build + 轮询 ==="
BUILD_ID=$($AWS codebuild start-build --project-name "$PROJ" --query "build.id" --output text)
echo "  BUILD_ID=$BUILD_ID"
while true; do
  ST=$($AWS codebuild batch-get-builds --ids "$BUILD_ID" --query "builds[0].buildStatus" --output text)
  echo "  [$(date +%H:%M:%S)] status=$ST"
  [ "$ST" = "SUCCEEDED" ] && { echo "✅ 镜像 ${VERSION} (${ARCH}) 烤制完成"; break; }
  case "$ST" in FAILED|FAULT|STOPPED|TIMED_OUT) echo "❌ 失败,看 CloudWatch logs(project=$PROJ)"; exit 1 ;; esac
  sleep 30
done

echo "=== 6. 验证产物(EROFS/gz 大小 + S3 版本)==="
$AWS s3 ls "s3://${BUCKET}/deployment/rootfs/" --human-readable | grep "$VERSION" || true
echo "  S3 对象版本(versioning 生效证明):"
$AWS s3api list-object-versions --bucket "$BUCKET" \
  --prefix "deployment/rootfs/openclaw-rootfs-${VERSION}.ext4.gz" \
  --query 'Versions[].{VersionId:VersionId,Size:Size,LastModified:LastModified}' --output table || true
echo ""
echo "🎉 demo 完成。镜像在 s3://${BUCKET}/deployment/rootfs/(版本 ${VERSION},${ARCH})。"
echo "   这是演示/教学链路,生产镜像仍走 stack.py 的 GoldenImageBuilder。"
