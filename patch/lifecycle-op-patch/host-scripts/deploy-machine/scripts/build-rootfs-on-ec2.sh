#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# build-rootfs-on-ec2.sh — Build the OpenClaw rootfs via a one-shot EC2
# builder, so the project can be brought up end-to-end from any machine
# (macOS, Windows, Cloud9, …) that has AWS CLI configured.
#
# This is the cloud-native equivalent of running `./build-rootfs.sh` locally.
# Use it when your laptop is not Linux (debootstrap is Linux-only) or when
# you simply want the build to happen in the same region as the deployment
# for faster S3 upload.
#
# Usage:
#   ./scripts/build-rootfs-on-ec2.sh [version] [arch]
#   ./scripts/build-rootfs-on-ec2.sh v1.0
#   ./scripts/build-rootfs-on-ec2.sh v1.0 arm64
#
# Flow:
#   1. Reads .env.deploy (REGION, PROFILE, ASSETS_BUCKET, HOST_INSTANCE_PROFILE_ARN).
#   2. Launches a single t3.medium (x86_64) or t4g.medium (arm64) Ubuntu host
#      in the same region, attached to the existing host instance profile so
#      it can write to the project's S3 bucket without bespoke IAM.
#   3. Waits for SSM Online, then runs the chroot build via Run Command.
#   4. The build finishes by uploading rootfs + data template + manifest.json
#      to s3://${ASSETS_BUCKET}/deployment/rootfs/.
#   5. Terminates the builder.
#
# Build time: ~8-12 minutes for x86_64. The script tails the SSM command
# output every 30s so you can watch progress.

set -euo pipefail

# ────────────────────────────────────────────────────────────────
# Inputs + sanity checks
# ────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"
VERSION="${1:-v1.0}"
ARCH_INPUT="${2:-x86_64}"

case "$ARCH_INPUT" in
  x86_64|amd64) ARCH="x86_64"; AMI_ARCH="amd64"; INSTANCE_TYPE="t3.medium"; AWSCLI_ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64";  AMI_ARCH="arm64"; INSTANCE_TYPE="t4g.medium"; AWSCLI_ARCH="aarch64" ;;
  *) echo "❌ unknown arch: $ARCH_INPUT (expected x86_64 or arm64)"; exit 1 ;;
esac

[ -f "$ENV_FILE" ] || { echo "❌ .env.deploy not found at $ENV_FILE — run ./setup.sh first"; exit 1; }
source "$ENV_FILE"

REGION="${REGION:?REGION not set in .env.deploy}"
PROFILE="${PROFILE:?PROFILE not set in .env.deploy}"
BUCKET="${ASSETS_BUCKET:?ASSETS_BUCKET not set in .env.deploy}"

AWS=(aws --profile "$PROFILE" --region "$REGION")

# ────────────────────────────────────────────────────────────────
# Resolve the latest Ubuntu Noble AMI for the chosen arch
# ────────────────────────────────────────────────────────────────

echo "→ Resolving Ubuntu Noble (${AMI_ARCH}) AMI in $REGION ..."
AMI_ID=$("${AWS[@]}" ec2 describe-images \
  --owners 099720109477 \
  --filters \
    "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${AMI_ARCH}-server-*" \
    "Name=state,Values=available" \
  --query "Images | sort_by(@, &CreationDate)[-1].ImageId" --output text)
[ -n "$AMI_ID" ] && [ "$AMI_ID" != "None" ] || { echo "❌ AMI lookup failed"; exit 1; }
echo "  AMI: $AMI_ID"

# ────────────────────────────────────────────────────────────────
# Reuse the project's host instance profile (already has S3 write +
# SSM core perms). If that's not present yet (CDK not deployed), bail.
# ────────────────────────────────────────────────────────────────

PROFILE_NAME="openclaw-host-profile"
"${AWS[@]}" iam get-instance-profile --instance-profile-name "$PROFILE_NAME" \
  >/dev/null 2>&1 \
  || { echo "❌ instance profile '$PROFILE_NAME' not found — run ./setup.sh first"; exit 1; }

# ────────────────────────────────────────────────────────────────
# Pick a default-VPC public subnet (any AZ)
# ────────────────────────────────────────────────────────────────

VPC_ID=$("${AWS[@]}" ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
[ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ] || { echo "❌ no default VPC in $REGION"; exit 1; }

SUBNET_ID=$("${AWS[@]}" ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=map-public-ip-on-launch,Values=true" \
  --query 'Subnets[0].SubnetId' --output text)
[ -n "$SUBNET_ID" ] && [ "$SUBNET_ID" != "None" ] || { echo "❌ no public subnet in default VPC"; exit 1; }
echo "  VPC: $VPC_ID, Subnet: $SUBNET_ID"

# ────────────────────────────────────────────────────────────────
# Disposable security group (egress only — SSM uses VPC endpoints
# or NAT, never inbound).
# ────────────────────────────────────────────────────────────────

SG_NAME="openclaw-rootfs-builder-$$"
SG_ID=$("${AWS[@]}" ec2 create-security-group \
  --group-name "$SG_NAME" --description "Temporary rootfs builder ($SG_NAME)" \
  --vpc-id "$VPC_ID" --query 'GroupId' --output text)
echo "  SG: $SG_ID"
trap '_cleanup' EXIT

INSTANCE_ID=""
_cleanup() {
  set +e
  if [ -n "$INSTANCE_ID" ]; then
    echo "→ terminating builder $INSTANCE_ID ..."
    "${AWS[@]}" ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null
    "${AWS[@]}" ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
  fi
  echo "→ deleting SG $SG_ID ..."
  "${AWS[@]}" ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null
}

# ────────────────────────────────────────────────────────────────
# Launch builder. user-data installs build deps, syncs the repo
# from S3, and waits for an SSM follow-up that triggers the build.
# ────────────────────────────────────────────────────────────────

# Ship the project tarball to S3 so the builder can sync it. Skipping
# .git, .venv, cdk.out, tests/__pycache__ keeps the upload small.
TARBALL="/tmp/openclaw-build-src-$$.tar.gz"
echo "→ packaging source tree → $TARBALL"
tar -C "$SCRIPT_DIR" \
  --exclude=".git" --exclude=".venv" --exclude="cdk.out" \
  --exclude="**/__pycache__" --exclude="*.pyc" \
  --exclude="blog*.docx" --exclude="*.bak" \
  -czf "$TARBALL" .
SRC_KEY="deployment/builder/src-$$.tar.gz"
"${AWS[@]}" s3 cp "$TARBALL" "s3://${BUCKET}/${SRC_KEY}" --quiet
rm -f "$TARBALL"

USER_DATA=$(cat <<'UDEOF'
#!/bin/bash
# Minimal user-data — the SSM Run Command installs the actual build deps,
# so this script only needs to mark "instance is ready for SSM".
exec > /var/log/rootfs-builder-init.log 2>&1
set -eux
echo "$(date -Iseconds) cloud-init done, awaiting SSM build trigger" \
  > /var/log/rootfs-builder-ready
UDEOF
)

echo "→ launching builder ($INSTANCE_TYPE)..."
INSTANCE_ID=$("${AWS[@]}" ec2 run-instances \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
  --user-data "$USER_DATA" \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=openclaw-rootfs-builder},{Key=openclaw:role,Value=builder}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "  Instance: $INSTANCE_ID"

echo "→ waiting for SSM Online ..."
for _ in $(seq 1 60); do
  status=$("${AWS[@]}" ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo "None")
  [ "$status" = "Online" ] && break
  sleep 10
done
[ "$status" = "Online" ] || { echo "❌ SSM didn't come online in 10 minutes"; exit 1; }
echo "  SSM Online"

# ────────────────────────────────────────────────────────────────
# Run the build via SSM. The .env.deploy is reproduced so
# build-rootfs.sh resolves $ASSETS_BUCKET / $REGION exactly the same
# way it would on a developer's laptop.
# ────────────────────────────────────────────────────────────────

echo "→ running build on $INSTANCE_ID (this takes ~8-12 minutes for x86_64) ..."

# Build the script as a single file on S3, then have SSM execute it.
# This avoids the shell-escaping nightmare of inlining a multi-line script
# into `aws ssm send-command --parameters commands=...`.
BUILD_SCRIPT="/tmp/openclaw-build-runner-$$.sh"
LOG_KEY="deployment/builder/log-$$-$(date +%Y%m%d-%H%M%S).txt"

cat > "$BUILD_SCRIPT" <<EOF
#!/bin/bash
# Tee everything (stdout + stderr) to /tmp/build.log so we can ship it back
# even when the build fails — SSM RunCommand otherwise truncates output.
exec > >(tee -a /tmp/build.log) 2>&1
trap 'rc=\$?; echo "--- runner.sh exited with rc=\$rc ---"; aws s3 cp /tmp/build.log "s3://${BUCKET}/${LOG_KEY}" --region ${REGION} --quiet 2>/dev/null || true; exit \$rc' EXIT

set -euxo pipefail

echo "=== runner.sh begin (\$(date -Iseconds)) ==="

# Wait for cloud-init's apt installs to finish.
for i in \$(seq 1 60); do
  [ -f /var/log/rootfs-builder-ready ] && break
  sleep 5
done

mkdir -p /opt/openclaw-build
cd /opt/openclaw-build
aws s3 cp "s3://${BUCKET}/${SRC_KEY}" /tmp/src.tar.gz --region ${REGION} --quiet
tar -xzf /tmp/src.tar.gz
rm -f /tmp/src.tar.gz

# Sanity: confirm key files exist before running the build.
echo "=== source tree top-level ==="
ls -la
echo "=== templates/ ==="
ls -la templates/ 2>&1 || echo "templates/ missing!"

# Reproduce the env the script expects.
cat > .env.deploy <<ENVEOF
REGION=${REGION}
PROFILE=
ASSETS_BUCKET=${BUCKET}
SKIP_MANIFEST=${SKIP_MANIFEST:-0}
ENVEOF

chmod +x build-rootfs.sh
SKIP_MANIFEST=${SKIP_MANIFEST:-0} ARCH=${ARCH} ./build-rootfs.sh ${VERSION}

echo "=== runner.sh end (\$(date -Iseconds)) ==="
EOF

# runner.sh is inlined into the SSM command as base64 instead of round-tripping
# through S3. The S3 path proved flaky (the builder intermittently 403'd fetching
# runner-$$.sh while the host role demonstrably could read the bucket — a timing/
# key issue in the upload+cleanup dance). Inlining removes that whole hop: the
# runner is decoded on the builder and executed directly. runner.sh is a few KB;
# base64 stays well under the SSM 64KB command limit.
RUNNER_B64=$(base64 < "$BUILD_SCRIPT" | tr -d '\n')
rm -f "$BUILD_SCRIPT"

# Build the bootstrap command: install deps, then decode+run the inlined runner.
SSM_CMD="set -x; cloud-init status --wait || true; for i in \$(seq 1 60); do fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/cache/apt/archives/lock >/dev/null 2>&1 || break; echo waiting-for-apt-lock; sleep 5; done; systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1 || true; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq -o DPkg::Lock::Timeout=300 debootstrap pigz e2fsprogs curl unzip jq; if ! command -v aws >/dev/null; then curl -sL 'https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}.zip' -o /tmp/awscliv2.zip && cd /tmp && unzip -qo awscliv2.zip && (./aws/install || ./aws/install --update); cd -; fi; aws --version; echo '${RUNNER_B64}' | base64 -d > /tmp/runner.sh; bash /tmp/runner.sh"

# Pass parameters via a temp JSON file (the command is large with the inlined runner).
SSM_PARAMS="/tmp/openclaw-ssm-params-$$.json"
python3 - "$SSM_CMD" > "$SSM_PARAMS" <<'PYEOF'
import json, sys
print(json.dumps({"commands": [sys.argv[1]]}))
PYEOF
CMD_ID=$("${AWS[@]}" ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "file://${SSM_PARAMS}" \
  --comment "openclaw rootfs build $VERSION ($ARCH)" \
  --timeout-seconds 1800 \
  --query 'Command.CommandId' --output text)
rm -f "$SSM_PARAMS"
echo "  CommandId: $CMD_ID"

# Cleanup hook (runner is now inlined into the SSM command, nothing to remove from S3).
trap '_cleanup' EXIT

# Tail status every 30s.
while true; do
  status=$("${AWS[@]}" ssm get-command-invocation \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  echo "  [$(date +%H:%M:%S)] status=$status"
  case "$status" in
    Success) break ;;
    Failed|TimedOut|Cancelled)
      echo "❌ build failed — fetching full log from S3 ..."
      "${AWS[@]}" s3 cp "s3://${BUCKET}/${LOG_KEY}" /tmp/openclaw-build-fail-$$.log 2>/dev/null \
        && tail -80 /tmp/openclaw-build-fail-$$.log \
        || echo "(log not in S3 yet — last 50 stderr lines from SSM:)"
      "${AWS[@]}" ssm get-command-invocation \
        --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
        --query 'StandardErrorContent' --output text | tail -50
      exit 1
      ;;
  esac
  sleep 30
done

echo ""
echo "✓ Build finished. Verifying S3 manifest ..."
"${AWS[@]}" s3 ls "s3://${BUCKET}/deployment/rootfs/" --human-readable

echo ""
echo "→ cleaning up source tarball s3://${BUCKET}/${SRC_KEY}"
"${AWS[@]}" s3 rm "s3://${BUCKET}/${SRC_KEY}" --quiet || true

echo ""
echo "🎉 rootfs ${VERSION} (${ARCH}) is now in S3."
echo "   Existing hosts will pick it up via init-host.sh's manifest retry loop;"
echo "   newly-launched ASG hosts will use it directly."
echo "   Trigger an explicit refresh with:"
echo "     curl -s -X POST \"\${API_URL}hosts/refresh-rootfs\" -H \"x-api-key: \${API_KEY}\" | jq ."
