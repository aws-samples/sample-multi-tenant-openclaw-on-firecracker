# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""manifest — ParamStore SecureString 分片编解码,纯函数零 boto3。

契约 (SPEC/specs/sqs-dispatch/interfaces.md):
- JSON-lines,一行一租户:{"t":<id>,"n":<vm_num>,"c":<vcpu>,"m":<mem_mb>,"e":<chat_ep 0|1>}
  可选:"g":<gateway_token_ct base64 KMS 密文>, "d":<paired.json base64>(#188)。
- 单 part < 3800 字节(ParamStore 标准 tier 上限 4KB 留余量给编解码)。
- 明文 secrets 一律不进(vkey/cognito password host 侧从 DDB tenants 自取)。g/d 是
  KMS 信封密文/公开配对元数据(公钥+deviceId,无私钥),整个 part 又是 SecureString
  再加密一层——host 用对的 EncryptionContext(token=tenant_id/device=owner_id)才解得开,
  跨租户密文进错 manifest line 也解不开(#188/铁律#11)。
- Poller 清理只允许精确 DeleteParameter 白名单 manifests/ 前缀,防连删 config。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

# ParamStore standard tier value 上限是 4096 字节;留 296 字节给编码/换行/JSON 结构冗余,
# 每 part 净 payload ≤3800 字节。超过必须开 advanced tier 或分片。
MANIFEST_PART_MAX_BYTES = 3800

# manifest line schema 允许字段。明文 secrets 一律不进;g/d 是密文/公开元数据(见模块
# docstring)。Guard 防以后有人往 manifest 里塞明文 vkey/password 之类。
#   g = gateway_token_ct(base64 KMS 密文, EncryptionContext=tenant_id, #187 P1)
#   d = device_paired_b64(base64 paired.json:deviceId+公钥+roles, 无私钥, #188)
#   r = restore_backup_key(S3 key,非密文/公开路径 slug;#199 restore 意图透传,
#       缺则 host launch 建空白盘=数据丢失。仅非空写,feature-off 行逐字节不变)
_ALLOWED_LINE_KEYS = frozenset({"t", "n", "c", "m", "e", "g", "d", "r"})


def encode_manifest_line(tenant: Dict[str, Any]) -> str:
    """把一个租户装箱条目编码成一行 JSON。字段名短(t/n/c/m/e/g/d)压 part 数。

    tenant 必填 tenant_id + vm_num + vcpu + mem_mb;chat_ep 缺省 False。
    g(gateway_token_ct)/d(device_paired_b64)可选:仅当非空才写入,feature-off
    的行与 pre-#188 逐字节一致(不新增空字段)。
    """
    tid = tenant.get("tenant_id") or tenant.get("t")
    if not tid:
        raise ValueError("manifest line: tenant_id required")
    vm_num = tenant.get("vm_num") if "vm_num" in tenant else tenant.get("n")
    if vm_num is None:
        raise ValueError("manifest line: vm_num required")
    vcpu = tenant.get("vcpu") if "vcpu" in tenant else tenant.get("c")
    mem_mb = tenant.get("mem_mb") if "mem_mb" in tenant else tenant.get("m")
    if vcpu is None or mem_mb is None:
        raise ValueError("manifest line: vcpu/mem_mb required")
    chat_ep = tenant.get("chat_ep", tenant.get("e", False))
    obj = {
        "t": str(tid),
        "n": int(vm_num),
        "c": int(vcpu),
        "m": int(mem_mb),
        "e": 1 if chat_ep else 0,
    }
    # #188 — 可选密文字段:仅非空写入(空 → 不加 key,保持 feature-off 行不变)。
    gw_ct = tenant.get("gateway_token_ct", tenant.get("g"))
    if gw_ct:
        obj["g"] = str(gw_ct)
    device_paired = tenant.get("device_paired_b64", tenant.get("d"))
    if device_paired:
        obj["d"] = str(device_paired)
    # #199 — restore 意图 key(仅非空写,feature-off 行逐字节不变)
    restore_key = tenant.get("restore_backup_key", tenant.get("r"))
    if restore_key:
        obj["r"] = str(restore_key)
    # sort_keys=False:字段顺序固定 t/n/c/m/e/g/d/r(可预测,便于 diff/日志)
    return json.dumps(obj, separators=(",", ":"))


def split_manifest_parts(
    lines: Iterable[str], max_bytes: int = MANIFEST_PART_MAX_BYTES
) -> List[str]:
    """把 JSON-lines 装进 ≤max_bytes 的 part(每 part 以 \\n 分隔)。

    单行超上限直接抛(不允许静默截断:租户装箱条目短且固定长度,超上限=schema bug)。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    parts: List[str] = []
    buf: List[str] = []
    buf_size = 0
    for line in lines:
        if not line:
            continue
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
            raise ValueError(
                f"manifest line exceeds max_bytes={max_bytes} ({line_bytes} B): {line[:80]!r}"
            )
        # +1 for the newline separator; first line has no separator overhead
        extra = line_bytes + (1 if buf else 0)
        if buf_size + extra > max_bytes:
            parts.append("\n".join(buf))
            buf = [line]
            buf_size = line_bytes
        else:
            buf.append(line)
            buf_size += extra
    if buf:
        parts.append("\n".join(buf))
    return parts


def decode_manifest_lines(part_body: str) -> List[Dict[str, Any]]:
    """解码一个 part 的 JSON-lines,校验字段白名单。空行忽略。"""
    out: List[Dict[str, Any]] = []
    for raw in part_body.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError(f"manifest line not a JSON object: {raw!r}")
        extra = set(obj.keys()) - _ALLOWED_LINE_KEYS
        if extra:
            # fail-loud:发现非白名单字段(比如有人往 manifest 里塞 vkey)立刻炸
            raise ValueError(f"manifest line has disallowed keys: {sorted(extra)}")
        out.append(obj)
    return out
