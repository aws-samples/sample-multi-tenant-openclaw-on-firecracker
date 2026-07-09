#!/bin/bash
# 在新账号用 CodeBuild 烤黄金镜像(arm64)。代码从本地仓库打包→S3 当 source(禁账号间拷数据)。
# 用法: ./build-golden-on-codebuild.sh <PROFILE> <ASSETS_BUCKET> <IMAGE_VERSION> [REGION]
#   ./build-golden-on-codebuild.sh <aws-profile> openclaw-assets-<AWS_ACCOUNT_ID> v1 <region>
set -euo pipefail
PROFILE="${1:?profile}"; BUCKET="${2:?assets bucket}"; VER="${3:?image version}"; REGION="${4:-ap-southeast-1}"
PROJ="openclaw-golden-image-builder"
ROLE_NAME="openclaw-codebuild-golden-role"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
AWS="aws --profile $PROFILE --region $REGION"
ACCT=$($AWS sts get-caller-identity --query Account --output text)

echo "=== 1. 打包本地仓库(只要烤镜像需要的,排除 .git/node_modules/cdk.out)→ S3 source ==="
SRC_ZIP="/tmp/openclaw-codebuild-src-${VER}.zip"
# allowlist 只打包烤镜像需要的;额外显式排除含真凭据的本地运维目录(judge安全门:
# .localbin/tenants-resp.network-response 含真 gateway_token,.remote-drift 含旧配置)
# + 任何 .pem/.key/凭据文件,双保险防 token 随 source 上传到新账号 S3。
( cd "$REPO_ROOT" && zip -rq "$SRC_ZIP" \
    build-rootfs.sh samples/finance-agent templates config.yml config.yml.example \
    deploy/codebuild deploy/userdata \
    scripts/lib \
    -x '*/node_modules/*' '*/.git/*' '*.bak*' '*.bak-*' '*/__pycache__/*' \
       '.localbin/*' '*/.localbin/*' '.remote-drift/*' '*/.remote-drift/*' \
       '*.pem' '*.key' '*tenants-resp*' '*cred*' '*secret*' )
# 验证 zip 内无凭据残留(fail-closed:有就中止)
if unzip -l "$SRC_ZIP" | grep -iE 'tenants-resp|\.pem|gateway_token|localbin|remote-drift'; then
  echo "❌ source zip 含疑似凭据文件,中止(防泄漏到新账号)"; exit 1
fi
$AWS s3 cp "$SRC_ZIP" "s3://${BUCKET}/codebuild-src/repo-${VER}.zip"
echo "源已上传 s3://${BUCKET}/codebuild-src/repo-${VER}.zip ($(du -h $SRC_ZIP|cut -f1))"

echo "=== 2. 建 CodeBuild service role(若无)==="
if ! $AWS iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  $AWS iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  # 烤镜像要:读 S3 source + 写 S3 deployment/rootfs + CloudWatch logs
  $AWS iam put-role-policy --role-name "$ROLE_NAME" --policy-name golden-build --policy-document "{
    \"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::${BUCKET}\",\"arn:aws:s3:::${BUCKET}/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"}
    ]}"
  sleep 8  # 等 role 生效
fi
ROLE_ARN="arn:aws:iam::${ACCT}:role/${ROLE_NAME}"

echo "=== 3. 建/更新 CodeBuild 项目(ARM aarch64,privileged,LARGE)==="
PROJ_CFG="{
  \"name\":\"${PROJ}\",
  \"source\":{\"type\":\"S3\",\"location\":\"${BUCKET}/codebuild-src/repo-${VER}.zip\",\"buildspec\":\"deploy/codebuild/buildspec-golden-image.yml\"},
  \"artifacts\":{\"type\":\"NO_ARTIFACTS\"},
  \"environment\":{
    \"type\":\"ARM_CONTAINER\",
    \"image\":\"aws/codebuild/amazonlinux2-aarch64-standard:3.0\",
    \"computeType\":\"BUILD_GENERAL1_LARGE\",
    \"privilegedMode\":true,
    \"environmentVariables\":[
      {\"name\":\"ASSETS_BUCKET\",\"value\":\"${BUCKET}\"},
      {\"name\":\"IMAGE_VERSION\",\"value\":\"${VER}\"},
      {\"name\":\"AWS_REGION\",\"value\":\"${REGION}\"}
    ]
  },
  \"serviceRole\":\"${ROLE_ARN}\"
}"
if $AWS codebuild batch-get-projects --names "$PROJ" --query "projects[0].name" --output text 2>/dev/null | grep -q "$PROJ"; then
  echo "$PROJ_CFG" > /tmp/proj.json && $AWS codebuild update-project --cli-input-json file:///tmp/proj.json >/dev/null
else
  echo "$PROJ_CFG" > /tmp/proj.json && $AWS codebuild create-project --cli-input-json file:///tmp/proj.json >/dev/null
fi

echo "=== 4. 触发 build + 等完成 ==="
BUILD_ID=$($AWS codebuild start-build --project-name "$PROJ" --query "build.id" --output text)
echo "BUILD_ID=$BUILD_ID,轮询中..."
while true; do
  ST=$($AWS codebuild batch-get-builds --ids "$BUILD_ID" --query "builds[0].buildStatus" --output text)
  echo "  status=$ST"
  [ "$ST" = "SUCCEEDED" ] && { echo "✅ 黄金镜像 ${VER} 烤制完成"; break; }
  [ "$ST" = "FAILED" -o "$ST" = "FAULT" -o "$ST" = "STOPPED" -o "$ST" = "TIMED_OUT" ] && { echo "❌ 失败,看 CloudWatch logs"; exit 1; }
  sleep 30
done
echo "=== 5. 验证 manifest ==="
$AWS s3 cp "s3://${BUCKET}/deployment/rootfs/manifest.json" -
