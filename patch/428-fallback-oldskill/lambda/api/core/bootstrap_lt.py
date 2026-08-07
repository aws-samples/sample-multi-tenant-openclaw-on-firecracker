# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#389 v2 块5 — 解析/改写 LaunchTemplate 里承载的 #389 s3-bootstrap user-data。

这是 `.claude/skills/claw-patch-skill/scripts/lt-userdata.py` 的 Lambda 侧对等物,但
只处理 #389+ 的 s3-bootstrap 形态(gzip-inline 是 pre-#389 的旧形态,控制面不生产也不
切换它),并且【同时】认 host 与 edge 两套 —— lt-userdata.py 的 `exec bash /...` 目标正则
只认 host,edge 是 `bash "/opt/openclaw-edge/<sha>/install-edge.sh"`,两者不同。

这一层保持无依赖(只 stdlib),所有 IO(describe / create-version / update-asg)都在 service
层做,故本文件每个函数都可纯函数单测。设计契约同 lt-userdata.py:

  · **摘要即地址。** #389 把内容 sha256 编进 S3 对象 key(deployment/bootstrap/<fleet>/<sha>/…),
    user-data 里下载的 key、`sha256sum -c` 的期望摘要必须是同一个值 —— 二者能漂移就等于
    "校验的是一个对象、下载的是另一个"。classify 强制 key 里的 sha == 期望 sha,否则 fail-closed。
  · **改写只动摘要。** promote 到另一个已存在的版本 = 把 user-data 里的旧 sha 全局换成新 sha
    (key、printf 期望摘要、edge 的 /opt/openclaw-edge/<sha> 解包目录都随之改),脚本其余每个
    字节保持不变,retry/ABANDON/串口日志语义原样保留,diff 可审计。
  · **绝不猜。** 任一标记(aws s3 cp / 期望 sha)出现次数 != 1 就拒绝:两个下载、两个摘要
    无法判定哪个权威,猜错就改掉了整个机队未来实例的启动路径。
"""

import re

LIMIT = 16384  # EC2 user-data 硬上限 16 KiB(#389 的整个意义就是让它随 tree 增长基本不变)

# fleet → (S3 前缀, bootstrap 对象名)。与 deploy/stacks/ha_edge.py 的
# _init_key / _edge_key 构造严格一致;改那边必须同步改这里(单测 test_389_bootstrap_lt 守着)。
FLEETS = {
    "host": {"prefix": "deployment/bootstrap/host/", "object": "init-host.sh"},
    "edge": {"prefix": "deployment/bootstrap/edge/", "object": "edge-bundle.tar.gz.b64"},
}

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# user-data 里唯一的 bootstrap 下载。bucket 名字符集按 S3 命名规则;key 必须落在
# deployment/bootstrap/<fleet>/<64hex>/ 下 —— 只认摘要寻址的不可变 key。
_S3_CP_RE = re.compile(
    r'aws s3 cp "s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/'
    r'(deployment/bootstrap/(?:host|edge)/[0-9a-f]{64}/[^"]+)"'
)
# 期望摘要:`printf '%s  %s\n' '<sha>' "$tmp" | sha256sum -c -`。解码后的文本里 \n 是字面
# 反斜杠+n(单引号内 shell 不转义,printf 才转),故正则匹配字面 \\n(同 lt-userdata.py)。
_SHA_EXPECT_RE = re.compile(r"printf '%s  %s\\n' '([0-9a-f]{64})'")


class BootstrapParseError(ValueError):
    """user-data 不是可识别的 #389 s3-bootstrap,或摘要绑定不自洽。fail-closed 专用。"""


def _exactly_one(matches, what):
    """返回唯一匹配,否则 fail loud。两个匹配一律不做消歧(同 lt-userdata.py 纪律)。"""
    if len(matches) != 1:
        raise BootstrapParseError(
            f"expected exactly 1 {what} in the s3 bootstrap, found {len(matches)}; "
            "refusing to guess which one is authoritative"
        )
    return matches[0]


def classify(text, fleet):
    """把一段 LT user-data 解析成 {bucket, key, sha256}。

    非 #389 s3-bootstrap、或 key 摘要与期望摘要不符 → BootstrapParseError。绝不对无法识别
    的启动路径做 pattern 猜测式改写。
    """
    if fleet not in FLEETS:
        raise BootstrapParseError(f"unknown fleet {fleet!r}; expected one of {sorted(FLEETS)}")
    prefix = FLEETS[fleet]["prefix"]
    if "aws s3 cp" not in text or "sha256sum -c -" not in text:
        raise BootstrapParseError(
            "user-data is not a #389 s3-bootstrap (no verified S3 download); refusing to "
            "operate on an unknown boot path"
        )
    # 只认落在本 fleet 前缀下的 bootstrap 下载(user-hook 可能另有 cp,不能误判)。
    cps = [(b, k) for (b, k) in _S3_CP_RE.findall(text) if k.startswith(prefix)]
    bucket, key = _exactly_one(cps, f"aws s3 cp of the {fleet} bootstrap object")
    sha = _exactly_one(_SHA_EXPECT_RE.findall(text), "expected sha256")
    # key 必须【逐字等于】deployment/bootstrap/<fleet>/<sha>/<规范对象名> —— 光校验前缀下有个 64hex
    # 目录不够:同目录里的【别的文件名】(init-host.sh 旁边塞个 x.sh)也会过前缀+摘要检查,却不是
    # 我们要的规范对象(codex 评审确认)。强制 == target_key(fleet, sha) 把对象名也钉死。
    expected_key = f"{prefix}{sha}/{FLEETS[fleet]['object']}"
    if key != expected_key:
        raise BootstrapParseError(
            f"key {key!r} is not the canonical digest-addressed object {expected_key!r}; the LT "
            "does not bind the expected immutable object (wrong object name or digest)"
        )
    return {"bucket": bucket, "key": key, "sha256": sha}


def target_key(fleet, sha):
    """给定 fleet + sha 拼出不可变对象 key(promote 目标)。与 ha_edge.py 构造一致。"""
    if fleet not in FLEETS:
        raise BootstrapParseError(f"unknown fleet {fleet!r}")
    if not _SHA_RE.match(sha):
        raise BootstrapParseError(f"{sha!r} is not a 64-hex sha256")
    spec = FLEETS[fleet]
    return f"{spec['prefix']}{sha}/{spec['object']}"


def rekey(text, fleet, new_sha):
    """把 s3-bootstrap 重指到同 fleet 的另一个不可变对象,其余字节不变。

    #389+ 版本切换 = 把 user-data 里的旧摘要全局换成新摘要:S3 key、printf 期望摘要、edge 的
    /opt/openclaw-edge/<sha> 解包目录都随之切换到新版本目录(天然幂等,新旧文件物理隔离)。
    返回改写后的 user-data;改写结果会被重新 classify 校验绑定新 key/sha,且 ≤16KiB。
    """
    if not _SHA_RE.match(new_sha):
        raise BootstrapParseError(f"{new_sha!r} is not a 64-hex sha256")
    info = classify(text, fleet)
    new_key = target_key(fleet, new_sha)
    # 旧 sha 是 key 的子串(deployment/bootstrap/<fleet>/<sha>/…),全局替换即把 key 一并切到
    # new_key;无需单独替换 key。下面 re-classify 会断言结果确实绑定了 new_key/new_sha。
    out = text.replace(info["sha256"], new_sha)
    check = classify(out, fleet)
    if check["key"] != new_key or check["sha256"] != new_sha:
        raise BootstrapParseError(
            "rewritten bootstrap does not bind the intended new key/sha; refusing to hand it "
            "to create-launch-template-version"
        )
    size = len(out.encode("utf-8"))
    if size > LIMIT:
        raise BootstrapParseError(f"rekeyed bootstrap {size}B exceeds {LIMIT}B")
    return out
