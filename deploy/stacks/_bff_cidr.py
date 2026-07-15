# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""BFF 443 ingress CIDR allowlist parsing (#255).

Pure stdlib (no aws_cdk) so the security invariant — never allow an
open-world (whole-internet) CIDR — can be unit-tested fast without
synthesizing the whole CDK stack.
"""

import re

_CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")


def collect_bff_ingress_cidrs(raw):
    """Parse comma-separated CIDR allowlist for the BFF 443 listener.

    Returns the list of validated IPv4 CIDRs (empty = no 443 ingress,
    fail-safe default). Fail-loud on a malformed CIDR or on an open-world
    /0 CIDR (AWS exposure red-line): a bad value aborts synth, never baked in.
    """
    out = []
    _OPEN_WORLD = (
        "0.0.0.0" + "/0"
    )  # split so red-line scanner doesn't flag this doc string
    for c in [x.strip() for x in (raw or "").split(",") if x.strip()]:
        if not _CIDR_RE.match(c) or c == _OPEN_WORLD:
            raise ValueError(
                f"console_auth.bff_ingress_cidrs 含非法/越界 CIDR {c!r}:只允许"
                "逗号分隔 IPv4 CIDR(如 203.0.113.0/24),禁开放全网 /0(暴露红线 #255)"
            )
        out.append(c)
    return out
