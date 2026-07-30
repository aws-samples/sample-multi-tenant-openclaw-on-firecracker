#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""List the API Gateway routes that invoke a function's $LATEST instead of an alias.

Why this matters enough to have its own tool: the Lambda lane's safety story is "update
$LATEST, verify it, and only then move the alias, so live traffic stays on the proven version
until the new one passes". That story holds only for callers that go THROUGH the alias.

Measured on the Singapore testbed: the private REST API integrates `ANY /` and `ANY /{proxy+}`
against the UNQUALIFIED function ARN. That is every request it serves. Those routes see the new
code the instant $LATEST is updated — before the verify runs. An operator who believes the alias
gate covers all traffic would be wrong about this environment, so the run says which routes
bypass it instead of implying a protection it does not have.

Read-only. Prints one `<api-id> <METHOD> <path>` line per unqualified route, nothing when every
route is alias-bound.

Usage: find-unqualified-routes.py <region> <function-name>
"""

import json
import subprocess
import sys


def aws(region, *args):
    """One place that shells out, so a failure is a failure rather than an empty result that
    reads like "nothing found"."""
    proc = subprocess.run(
        ["aws", "--region", region, *args, "--output", "json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "null")


def unqualified_routes(region, function_name):
    # An integration URI ending in `:function:<name>/invocations` carries no qualifier, so the
    # call lands on $LATEST. One ending in `:function:<name>:<alias>/invocations` follows the
    # alias. Matching the exact suffix (rather than searching for the name) keeps
    # `openclaw-api-worker` from matching `openclaw-api`.
    unqualified_suffix = f":function:{function_name}/invocations"
    found = []
    for api in aws(region, "apigateway", "get-rest-apis")["items"]:
        for res in aws(
            region, "apigateway", "get-resources", "--rest-api-id", api["id"]
        )["items"]:
            for method in sorted((res.get("resourceMethods") or {})):
                integration = aws(
                    region,
                    "apigateway",
                    "get-integration",
                    "--rest-api-id",
                    api["id"],
                    "--resource-id",
                    res["id"],
                    "--http-method",
                    method,
                )
                if str(integration.get("uri") or "").endswith(unqualified_suffix):
                    found.append(f"{api['id']} {method} {res['path']}")
    return found


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    region, function_name = argv[1], argv[2]
    try:
        routes = unqualified_routes(region, function_name)
    except RuntimeError as exc:
        # Fail loudly rather than printing nothing: "no unqualified routes" and "could not
        # check" must not look identical to the caller.
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    for route in routes:
        print(route)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
