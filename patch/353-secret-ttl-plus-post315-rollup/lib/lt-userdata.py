#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# lt-userdata.py — the reliable way to touch an LT-baked userdata script (init-host.sh) WITHOUT
# hand-wrangling base64/gzip/the 16KB limit. Mirrors CDK's exact packing (gzip(9) -> base64 -> a
# fixed 4-line bootstrap that decodes and execs).
#
#   decode <bootstrap-file>              # LT bootstrap -> plaintext init-host.sh on stdout
#   repack <plaintext-init-host.sh>      # plaintext -> LT bootstrap on stdout (16KB-checked)
#     repack --strip <plaintext>         # ALSO strip comments/blank lines (CDK's FIRST-bake behavior)
#
# HOT-PATCH contract (Codex 2026-07-22 #6): in a hot patch you `decode` the LIVE bootstrap, edit the
# plaintext, then `repack`. The decoded content is ALREADY stripped (CDK stripped it at first bake),
# so repack must NOT strip again — a second strip silently eats heredoc bodies / continued lines.
# Therefore repack defaults to NO strip (byte-faithful). `--strip` is only for CDK's very first bake
# from a fully-commented source, never in a hot patch.
#
# Placeholder guard (Codex #8): BOTH decode and repack REFUSE unresolved CDK tokens such as
# {{PLACEHOLDER}}. Literal brace pairs used by shell/application content are allowed.
import base64
import gzip
import re
import sys

LIMIT = 16384
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
# The exact 4-line bootstrap CDK emits (and repack emits). decode does a strict fullmatch on this so a
# fake blob hidden in a comment / a second payload can't be picked up (Codex #12).
_BOOT_RE = re.compile(
    r"#!/bin/bash\n"
    r"set -e\n"
    r"echo ([A-Za-z0-9+/]+={0,2}) \| base64 -d \| gunzip > /tmp/init-host\.sh\n"
    r"exec bash /tmp/init-host\.sh\n?\Z"
)


def _refuse_placeholders(text: str, where: str):
    match = _PLACEHOLDER_RE.search(text)
    if match:
        sys.stderr.write(
            f"{where}: content contains unresolved template placeholder {match.group(0)!r} — "
            "this is raw CDK template content, not rendered UserData. Refusing.\n"
        )
        sys.exit(2)


def strip_for_userdata(script: str) -> str:
    # heredoc-aware: keep heredoc bodies + shebang; drop full-line comments and blank lines.
    # ONLY used for CDK's first bake (--strip); never in a hot-patch repack.
    out, heredoc = [], None
    for line in script.splitlines():
        if heredoc is not None:
            out.append(line)
            if line.strip() == heredoc:
                heredoc = None
            continue
        m = re.search(r"""<<[-]?\s*(["'\\]?)([A-Za-z_][A-Za-z0-9_]*)\1""", line)
        if m:
            heredoc = m.group(2)
            out.append(line)
            continue
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not s.startswith("#!"):
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def decode(data: bytes) -> bytes:
    """Strict: the whole input must be exactly the CDK 4-line bootstrap with ONE base64 payload."""
    text = data.decode("utf-8", "strict").strip("\n") + "\n"
    m = _BOOT_RE.fullmatch(text)
    if not m:
        sys.stderr.write(
            "decode: input is not exactly the CDK 4-line bootstrap "
            "(#!/bin/bash / set -e / echo <b64> | base64 -d | gunzip > /tmp/init-host.sh / exec bash ...). "
            "Refusing — won't pick a blob out of a comment or a second payload.\n"
        )
        sys.exit(2)
    try:
        raw = gzip.decompress(base64.b64decode(m.group(1), validate=True))
    except Exception as e:  # noqa: BLE001 — any decode/gunzip failure is fatal, report it
        sys.stderr.write(f"decode: base64/gunzip failed: {e}\n")
        sys.exit(2)
    _refuse_placeholders(raw.decode("utf-8", "replace"), "decode")
    return raw


def repack(script: str, do_strip: bool = False) -> str:
    _refuse_placeholders(script, "repack")
    body = strip_for_userdata(script) if do_strip else script
    # mtime=0 so the gzip byte stream is deterministic across Python versions/runs (Codex #15).
    blob = base64.b64encode(
        gzip.compress(body.encode("utf-8"), compresslevel=9, mtime=0)
    ).decode("ascii")
    boot = (
        "#!/bin/bash\nset -e\n"
        f"echo {blob} | base64 -d | gunzip > /tmp/init-host.sh\n"
        "exec bash /tmp/init-host.sh\n"
    )
    n = len(boot.encode())
    if n > LIMIT:
        sys.stderr.write(
            f"repack: bootstrap {n}B exceeds 16KB even gzipped — move the body to an S3 bootstrap.\n"
        )
        sys.exit(2)
    sys.stderr.write(f"repack: OK, {n}B (<{LIMIT}), strip={do_strip}\n")
    return boot


def read(arg):
    return sys.stdin.buffer.read() if arg == "-" else open(arg, "rb").read()


if __name__ == "__main__":
    argv = sys.argv[1:]
    do_strip = "--strip" in argv
    argv = [a for a in argv if a != "--strip"]
    if len(argv) < 2 or argv[0] not in ("decode", "repack"):
        sys.stderr.write(__doc__ or "")
        sys.exit(2)
    verb, src = argv[0], argv[1]
    if verb == "decode":
        sys.stdout.buffer.write(decode(read(src)))
    else:
        sys.stdout.write(repack(read(src).decode("utf-8"), do_strip))
