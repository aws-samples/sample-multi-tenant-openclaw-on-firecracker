#!/usr/bin/env bash
# snapshot-version.sh — #217 V2 增量2:给 assets 桶「打一个版本快照」。
#
# 扫 deployment/ 下【全部】对象(rootfs 镜像 + scripts 运维脚本 + edge + litellm
# + monitoring,共 5 类子目录),采集每个文件的 {path, s3_version_id, etag},
# 写一条快照到 DDB openclaw-version-snapshots(主键 snapshot_time=ISO8601 时间字符串)。
#
# 为什么记整个 deployment/ 而不只 rootfs+scripts(owner 2026-07-14 设计理由):
# deployment/ 下每一类文件(镜像/脚本/edge lua/litellm/monitoring)都可能被单独改动;
# 若快照只挑镜像+脚本,以后改了 edge 或 litellm 的某文件,快照就漏记 → 回滚时缺文件、
# 版本不一致。宁可全记不漏:一条快照 = 此刻【整套 deployment】的完整定格,pull-image
# 后续按 snapshot_time 照它逐文件按精确 VersionId 拉,保证回滚到的是完整无缺失的一版。
#
# VersionId=null(versioning 开启前就存在的对象)可正常 get(实测 --version-id null
# 能拉到完整内容),只是没有更早历史——快照记 null 就拉 null,精确对应当时内容,无害。
#
# 为什么打快照是独立脚本(非塞进 setup.sh):打快照是「此刻定格一个可回滚版本」的
# 显式运维动作(答 to_discuss Q7)——setup.sh 推文件后可调用它,也可单独手动打。
#
# 完整性标识用 S3 ETag(非现算 sha256):大镜像是 multipart 上传,ETag 带 -N 后缀不是
# 纯 MD5,但「ETag 变 = 内容变」足够做完整性锚点;免下载 1GB 现算 hash(不现实)。
# 若将来要强 sha256,由 build-rootfs 产出时算好、随 manifest 带上,这里读取即可。
#
# 用法: ./scripts/snapshot-version.sh <BUCKET> <REGION> [LABEL] [--profile P]
#   ./scripts/snapshot-version.sh openclaw-assets-<ACCOUNT_ID> ap-southeast-1 v1.0
set -euo pipefail
export AWS_PAGER=""

BUCKET="${1:?用法: snapshot-version.sh <BUCKET> <REGION> [LABEL] [--profile P]}"
REGION="${2:?缺 REGION}"
LABEL="${3:-}"                       # 可选人类可读标签;缺省则自动填 rootfs 版本(见下)
PROFILE_FLAG=""
[ "${4:-}" = "--profile" ] && PROFILE_FLAG="--profile ${5:?--profile 后缺 profile 名}"

# #217 — LABEL 缺省 → 自动读 deployment/rootfs/manifest.json 的 version 当 label
# (owner 2026-07-14:快照 label 记 rootfs 版本,console 显示 "snapshot_time(rootfs版)")。
# 显式传了 LABEL 就用传的;读不到 manifest(空桶/无 versioning)也不致命,label 留空。
if [ -z "${LABEL}" ]; then
  # shellcheck disable=SC2086
  LABEL="$(aws s3 cp "s3://${BUCKET}/deployment/rootfs/manifest.json" - --region "${REGION}" ${PROFILE_FLAG} 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version",""))' 2>/dev/null || echo "")"
  [ -n "${LABEL}" ] && echo "  label 未传 → 自动用 rootfs 版本: ${LABEL}"
fi

# 只扫 deployment/ 下(镜像 rootfs + 运维脚本 scripts);skills/ 不纳入——它不在
# deployment 下,且由 host 每 5min cron 自同步,不属"版本化交付产物"(owner 2026-07-14)。
PREFIXES=("deployment/")
TABLE="openclaw-version-snapshots"

echo "== 打版本快照:s3://${BUCKET} → DDB ${TABLE} (label=${LABEL:-<none>}) =="

# 采集每个前缀下所有对象的 key + 当前 VersionId + ETag。让 python 直接调
# aws(每前缀一次干净 JSON),合并——不做脆弱的字符串拼接(踩过 ][ 边界 bug)。
FILES_JSON="$(BUCKET="$BUCKET" REGION="$REGION" PREFIXES="${PREFIXES[*]}" \
  PROFILE_FLAG="$PROFILE_FLAG" python3 <<'PY'
import json, os, subprocess
bucket, region = os.environ["BUCKET"], os.environ["REGION"]
prefixes = os.environ["PREFIXES"].split()
extra = os.environ.get("PROFILE_FLAG", "").split()
merged = []
for pfx in prefixes:
    cmd = ["aws", "s3api", "list-object-versions", "--bucket", bucket,
           "--prefix", pfx, "--region", region,
           "--query", "Versions[?IsLatest==`true`].{path:Key,s3_version_id:VersionId,etag:ETag}",
           "--output", "json"] + extra
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        continue
    parsed = json.loads(out.stdout)  # 空前缀(如 skills/)query 返回 null → None
    if parsed:
        merged.extend(parsed)
print(json.dumps(merged))
PY
)"

COUNT="$(printf '%s' "$FILES_JSON" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
[ "$COUNT" -gt 0 ] || { echo "✗ 采集到 0 个文件——桶/前缀不对或权限问题,拒写空快照(fail-loud)" >&2; exit 1; }
echo "  采集 ${COUNT} 个文件(整个 deployment/ 的当前版本:rootfs+scripts+edge+litellm+monitoring)"

# ISO8601 时间字符串主键(owner 2026-07-14 改:human 可读 + 字典序=时间序)。
# 精确到秒 + Z(UTC);如 2026-07-14T08:15:30Z。秒级足够(同秒两次快照极罕见,
# 真撞则第二次覆盖第一次——运维打快照不会同秒连打)。
SNAP_TS="$(python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')"

# 组装 DDB item(snapshot_time:S(ISO) + files:S(JSON 字符串) + label:S + file_count:N)
ITEM="$(python3 -c "
import json, sys
files = json.load(sys.stdin)
item = {
  'snapshot_time': {'S': '${SNAP_TS}'},
  'files': {'S': json.dumps(files, separators=(',',':'))},
  'file_count': {'N': str(len(files))},
}
if '${LABEL}':
    item['label'] = {'S': '${LABEL}'}
print(json.dumps(item))
" <<<"$FILES_JSON")"

# shellcheck disable=SC2086
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" $PROFILE_FLAG \
  --item "$ITEM"

echo "  ✓ 快照已写:snapshot_time=${SNAP_TS}${LABEL:+ (label=${LABEL})}, ${COUNT} 文件"
echo "  pull-image 后续可用 ?snapshot_time=${SNAP_TS} 照此拉全套(增量3)。"
echo "SNAP_TS=${SNAP_TS}"
