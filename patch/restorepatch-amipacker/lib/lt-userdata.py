#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# lt-userdata.py — safely decode and repack the rendered init-host.sh carried by a
# Launch Template without hand-wrangling base64/gzip or the EC2 16 KiB limit.
#
#   inspect <bootstrap-file>             # report which bootstrap form the LT carries
#   decode <bootstrap-file>              # gzip-inline bootstrap -> plaintext init-host.sh
#   repack <plaintext-init-host.sh>      # plaintext -> gzip-inline bootstrap
#   repack --strip <plaintext>           # strip comments/blanks for the first CDK bake only
#   rekey <bootstrap-file> <key> <sha>   # s3-bootstrap -> same bootstrap, new key + digest
#
# Two bootstrap forms exist in the wild and both must be operable:
#   gzip-inline   pre-#389 — the whole script is a gzip+base64 blob inside user-data.
#   s3-bootstrap  #389+    — user-data downloads an immutable S3 object and verifies its
#                            full SHA256 before exec. The script never enters user-data.
# `inspect` is the entry point: it classifies without mutating, so the shell driver knows
# whether to decode a blob or to fetch the S3 object. This file stays dependency-free
# (no boto3, no aws CLI) so every function is unit-testable; all IO belongs to the caller.
import base64
import gzip
import json
import re
import sys


LIMIT = 16384
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
_BOOT_RE = re.compile(
    r"#!/bin/bash\n"
    r"set -e\n"
    r"echo ([A-Za-z0-9+/]+={0,2}) \| base64 -d \| gunzip > /tmp/init-host\.sh\n"
    r"exec bash /tmp/init-host\.sh\n?\Z"
)
# s3-bootstrap markers. Each must match EXACTLY once: a bootstrap carrying two S3
# downloads or two digests is ambiguous, and guessing which one is authoritative is
# how a patch lands on the wrong object.
_S3_CP_RE = re.compile(
    r'aws s3 cp "s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/(\S+?)" '
)
_SHA_EXPECT_RE = re.compile(r"printf '%s  %s\\n' '([0-9a-f]{64})'")
_EXEC_RE = re.compile(r"^exec bash (/\S+)$", re.MULTILINE)


def _refuse_placeholders(text: str, where: str) -> None:
    match = _PLACEHOLDER_RE.search(text)
    if match:
        sys.stderr.write(
            f"{where}: content contains unresolved template placeholder "
            f"{match.group(0)!r}; refusing raw CDK template content.\n"
        )
        raise SystemExit(2)


def strip_for_userdata(script: str) -> str:
    """Apply the CDK first-bake comment/blank-line stripper."""
    out: list[str] = []
    heredoc = None
    for line in script.splitlines():
        if heredoc is not None:
            out.append(line)
            if line.strip() == heredoc:
                heredoc = None
            continue
        marker = re.search(
            r"""<<[-]?\s*(["'\\]?)([A-Za-z_][A-Za-z0-9_]*)\1""", line
        )
        if marker:
            heredoc = marker.group(2)
            out.append(line)
            continue
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#!")):
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _exactly_one(pattern: "re.Pattern[str]", text: str, what: str):
    """Return the sole match, or fail loudly. Two matches are never disambiguated."""
    found = pattern.findall(text)
    if len(found) != 1:
        sys.stderr.write(
            f"inspect: expected exactly 1 {what} in the s3 bootstrap, found "
            f"{len(found)}; refusing to guess which one is authoritative.\n"
        )
        raise SystemExit(2)
    return found[0]


def classify(text: str) -> dict:
    """Classify a rendered LT user-data into one of the two known bootstrap forms.

    Returns {"form": "gzip-inline"} or {"form": "s3-bootstrap", bucket, key, sha256,
    target}. Anything else is fatal: an unrecognized bootstrap must never be patched
    by pattern-guessing, because a wrong guess rewrites the boot path of every future
    host in the fleet.
    """
    if _BOOT_RE.fullmatch(text):
        return {"form": "gzip-inline"}
    if "aws s3 cp" in text and "sha256sum -c -" in text:
        bucket, key = _exactly_one(_S3_CP_RE, text, "aws s3 cp of the init object")
        sha = _exactly_one(_SHA_EXPECT_RE, text, "expected sha256")
        target = _exactly_one(_EXEC_RE, text, "exec bash target")
        # The key must carry the same digest it is verified against; #389 makes the
        # content hash part of the key precisely so the two cannot drift apart.
        if sha not in key:
            sys.stderr.write(
                f"inspect: expected sha256 {sha} is absent from object key {key!r}; "
                "the LT does not bind an immutable digest-addressed object.\n"
            )
            raise SystemExit(2)
        return {
            "form": "s3-bootstrap",
            "bucket": bucket,
            "key": key,
            "sha256": sha,
            "target": target,
        }
    sys.stderr.write(
        "inspect: user-data matches neither the gzip-inline bootstrap nor the "
        "s3-bootstrap form; refusing to operate on an unknown boot path.\n"
    )
    raise SystemExit(2)


def rekey(text: str, new_key: str, new_sha: str) -> str:
    """Repoint an s3-bootstrap at a new immutable object, changing nothing else.

    This is the #389+ equivalent of repack: the script body never enters user-data, so
    rolling a new init-host.sh means swapping the object key and the expected digest.
    Every other byte of the bootstrap is preserved, which keeps the diff auditable and
    leaves the retry/ABANDON/serial-console semantics exactly as deployed.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", new_sha):
        sys.stderr.write(f"rekey: {new_sha!r} is not a 64-hex sha256.\n")
        raise SystemExit(2)
    if new_sha not in new_key:
        sys.stderr.write(
            f"rekey: new key {new_key!r} must contain the new digest {new_sha} so the "
            "object stays immutable and digest-addressed (#389 DoD).\n"
        )
        raise SystemExit(2)
    info = classify(text)
    if info["form"] != "s3-bootstrap":
        sys.stderr.write(
            f"rekey: user-data is {info['form']}, not s3-bootstrap; use repack instead.\n"
        )
        raise SystemExit(2)
    out = text.replace(
        f's3://{info["bucket"]}/{info["key"]}', f's3://{info["bucket"]}/{new_key}'
    ).replace(info["sha256"], new_sha)
    # Re-classify the result: the rewrite must produce a bootstrap that still parses
    # and now binds the intended pair, or we do not hand it to create-launch-template-version.
    check = classify(out)
    if check["key"] != new_key or check["sha256"] != new_sha:
        sys.stderr.write("rekey: rewritten bootstrap does not bind the new key/sha.\n")
        raise SystemExit(2)
    size = len(out.encode())
    if size > LIMIT:
        sys.stderr.write(f"rekey: bootstrap {size}B exceeds {LIMIT}B.\n")
        raise SystemExit(2)
    sys.stderr.write(f"rekey: OK, {size}B (<{LIMIT}), key={new_key}\n")
    return out


def decode(data: bytes) -> bytes:
    """Decode exactly one canonical four-line CDK bootstrap."""
    text = data.decode("utf-8", "strict")
    match = _BOOT_RE.fullmatch(text)
    if not match:
        form = "unknown"
        if "aws s3 cp" in text and "sha256sum -c -" in text:
            form = "s3-bootstrap (#389+)"
        sys.stderr.write(
            "decode: input is not exactly the canonical four-line CDK bootstrap "
            f"(looks like: {form}); refusing to select a payload from arbitrary text. "
            "For an s3-bootstrap, run `inspect` and fetch the S3 object instead.\n"
        )
        raise SystemExit(2)
    try:
        raw = gzip.decompress(base64.b64decode(match.group(1), validate=True))
    except Exception as exc:  # noqa: BLE001 - every decoding error is fatal
        sys.stderr.write(f"decode: base64/gunzip failed: {exc}\n")
        raise SystemExit(2) from exc
    _refuse_placeholders(raw.decode("utf-8", "replace"), "decode")
    return raw


def repack(script: str, do_strip: bool = False) -> str:
    _refuse_placeholders(script, "repack")
    body = strip_for_userdata(script) if do_strip else script
    blob = base64.b64encode(
        gzip.compress(body.encode("utf-8"), compresslevel=9, mtime=0)
    ).decode("ascii")
    bootstrap = (
        "#!/bin/bash\n"
        "set -e\n"
        f"echo {blob} | base64 -d | gunzip > /tmp/init-host.sh\n"
        "exec bash /tmp/init-host.sh\n"
    )
    size = len(bootstrap.encode())
    if size > LIMIT:
        sys.stderr.write(
            f"repack: bootstrap {size}B exceeds {LIMIT}B; move the body to an "
            "S3 bootstrap.\n"
        )
        raise SystemExit(2)
    sys.stderr.write(f"repack: OK, {size}B (<{LIMIT}), strip={do_strip}\n")
    return bootstrap


def read(path: str) -> bytes:
    return sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()


_USAGE = (
    "usage: lt-userdata.py inspect <bootstrap>\n"
    "       lt-userdata.py decode <bootstrap>\n"
    "       lt-userdata.py repack [--strip] <plaintext>\n"
    "       lt-userdata.py rekey <bootstrap> <new-key> <new-sha256>\n"
)


if __name__ == "__main__":
    argv = sys.argv[1:]
    do_strip = "--strip" in argv
    argv = [arg for arg in argv if arg != "--strip"]
    if not argv or argv[0] not in ("decode", "repack", "inspect", "rekey"):
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    verb = argv[0]
    if verb == "rekey":
        if len(argv) != 4:
            sys.stderr.write(_USAGE)
            raise SystemExit(2)
        sys.stdout.write(
            rekey(read(argv[1]).decode("utf-8"), argv[2], argv[3])
        )
    elif len(argv) != 2:
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    elif verb == "inspect":
        # JSON so the shell driver can read fields with python3 -c, no jq dependency.
        sys.stdout.write(
            json.dumps(classify(read(argv[1]).decode("utf-8")), sort_keys=True) + "\n"
        )
    elif verb == "decode":
        sys.stdout.buffer.write(decode(read(argv[1])))
    else:
        sys.stdout.write(repack(read(argv[1]).decode("utf-8"), do_strip))
