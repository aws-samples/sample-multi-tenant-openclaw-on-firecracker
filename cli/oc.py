#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""oc — OpenClaw command-line interface (issue #21).

Single-file Python tool that wraps the orchestrator's REST API.
Reads `OC_API_URL` and `OC_API_KEY` from the environment, falling
back to a sibling `.env.deploy` file (the file emitted by setup.sh).

Why standard library only?
- Easy to install (just symlink); no venv / pip requirement.
- argparse + urllib.request cover everything we need.

Usage:

    oc list
    oc get t1
    oc create demo --vcpu 4 --mem-mb 8192
    oc restart t1
    oc delete t1 --force
    oc backups
    oc hosts
    oc version
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.6.0"  # bumped per issue tag (v0.6.0-issue21)

ACTION_VERBS = ("restart", "start", "stop", "pause", "resume", "backup", "reset")


# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════


def _load_env_deploy_file():
    """Walk up looking for .env.deploy and parse it as KEY=VALUE pairs."""
    here = Path.cwd()
    for parent in (here, *here.parents):
        f = parent / ".env.deploy"
        if f.is_file():
            env = {}
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
            return env
    return {}


def _load_env():
    """Resolve (api_url, api_key) from env or .env.deploy."""
    file_env = _load_env_deploy_file()
    api_url = os.environ.get("OC_API_URL") or file_env.get("API_URL", "")
    api_key = os.environ.get("OC_API_KEY") or file_env.get("API_KEY", "")
    return api_url, api_key


def _build_url(base, endpoint):
    """Join base + endpoint without producing double slashes."""
    if base.endswith("/"):
        base = base[:-1]
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return base + endpoint


# ═══════════════════════════════════════════
# HTTP helper
# ═══════════════════════════════════════════


def _request(method, endpoint, body=None):
    """Send a request to the orchestrator API.

    Returns (status_code, body_bytes). Raises on missing credentials.
    """
    api_url, api_key = _load_env()
    if not api_url or not api_key:
        raise RuntimeError(
            "missing credentials: set OC_API_URL and OC_API_KEY "
            "(or run setup.sh to populate .env.deploy)")
    url = _build_url(api_url, endpoint)
    data = None
    headers = {"X-api-key": api_key}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def _fmt_response(payload):
    try:
        obj = json.loads(payload)
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)


# ═══════════════════════════════════════════
# Subcommands
# ═══════════════════════════════════════════


def cmd_list(_args):
    _, body = _request("GET", "/tenants")
    print(_fmt_response(body))
    return 0


def cmd_get(args):
    _, body = _request("GET", f"/tenants/{args.id}")
    print(_fmt_response(body))
    return 0


def cmd_create(args):
    payload = {"name": args.name}
    if args.vcpu is not None:
        payload["vcpu"] = args.vcpu
    if args.mem_mb is not None:
        payload["mem_mb"] = args.mem_mb
    if args.config_template:
        payload["config_template"] = args.config_template
    _, body = _request("POST", "/tenants", body=payload)
    print(_fmt_response(body))
    return 0


def cmd_delete(args):
    if not args.force:
        try:
            ans = input(f"Delete tenant {args.id}? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return 1
    keep = "true" if args.keep_data else "false"
    _, body = _request("DELETE", f"/tenants/{args.id}?keep_data={keep}")
    print(_fmt_response(body))
    return 0


def cmd_action(args):
    _, body = _request("POST", f"/tenants/{args.id}/{args.action}")
    print(_fmt_response(body))
    return 0


def cmd_backups(_args):
    _, body = _request("GET", "/backups")
    print(_fmt_response(body))
    return 0


def cmd_hosts(_args):
    _, body = _request("GET", "/hosts")
    print(_fmt_response(body))
    return 0


def cmd_version(_args):
    print(f"oc {VERSION}")
    return 0


# ═══════════════════════════════════════════
# argparse wiring
# ═══════════════════════════════════════════


def _build_parser():
    p = argparse.ArgumentParser(prog="oc", description="OpenClaw orchestrator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List all tenants").set_defaults(func=cmd_list)

    g = sub.add_parser("get", help="Show one tenant"); g.add_argument("id"); g.set_defaults(func=cmd_get)

    c = sub.add_parser("create", help="Create a tenant")
    c.add_argument("name")
    c.add_argument("--vcpu", type=int)
    c.add_argument("--mem-mb", type=int)
    c.add_argument("--config-template", default="")
    c.set_defaults(func=cmd_create)

    d = sub.add_parser("delete", help="Delete a tenant")
    d.add_argument("id")
    d.add_argument("--force", action="store_true", help="Skip confirmation")
    d.add_argument("--keep-data", action="store_true", default=True,
                   help="Keep the data EBS volume (default: yes)")
    d.set_defaults(func=cmd_delete)

    for verb in ACTION_VERBS:
        a = sub.add_parser(verb, help=f"{verb} a tenant's VM")
        a.add_argument("id")
        a.set_defaults(func=cmd_action, action=verb)

    sub.add_parser("backups", help="List all backups").set_defaults(func=cmd_backups)
    sub.add_parser("hosts", help="List all hosts").set_defaults(func=cmd_hosts)
    sub.add_parser("version", help="Show CLI version").set_defaults(func=cmd_version)

    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = str(e)
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
