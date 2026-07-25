#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regenerate tests/fixtures/stack_logical_ids.json (T3-4 Phase 0 guardrail).

Run from the repo root:  uv run python tests/fixtures/gen_logical_ids.py

Only regenerate when a change INTENTIONALLY alters the stack's resources, and
call the diff out in review — the snapshot exists to catch accidental
logical-ID drift (which makes CFN replace resources) during the stack.py
_build_* refactor.
"""

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def main():
    sys.path.insert(0, str(ROOT / "deploy"))
    import aws_cdk as cdk
    from aws_cdk import assertions

    # The snapshot must reflect the CANONICAL config (config.yml.example) — the
    # same one conftest materializes for the test session and CI synthesizes
    # against. A developer's local config.yml (features enabled, prod values)
    # would otherwise bake machine-specific resources into the fixture and
    # break CI. Force config.yml.example for the duration, restoring any
    # existing local config.yml afterward.
    cfg = ROOT / "config.yml"
    backup = ROOT / "config.yml.genbak"
    had_local = cfg.exists()
    if had_local:
        shutil.copyfile(cfg, backup)
    shutil.copyfile(ROOT / "config.yml.example", cfg)
    try:
        if "stack" in sys.modules:
            del sys.modules["stack"]
        from stack import OpenClawOrchestratorStack
        app = cdk.App()
        s = OpenClawOrchestratorStack(
            app, "OpenClawOrchestrator",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
        tmpl = assertions.Template.from_stack(s).to_json()
        snap = {lid: r["Type"]
                for lid, r in sorted(tmpl.get("Resources", {}).items())}
        out = ROOT / "tests" / "fixtures" / "stack_logical_ids.json"
        out.write_text(json.dumps(snap, indent=2) + "\n")
        print(f"wrote {out} with {len(snap)} resources")
    finally:
        if had_local:
            shutil.copyfile(backup, cfg)
            backup.unlink()
        else:
            cfg.unlink()


if __name__ == "__main__":
    main()
