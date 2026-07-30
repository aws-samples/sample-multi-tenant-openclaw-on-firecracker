# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


PATCH = Path(__file__).resolve().parents[1]
PREPARE = PATCH / "factory" / "scripts" / "prepare.sh"


def environment() -> dict[str, object]:
    return {
        "account": "111111" + "111111",
        "region": "us-east-1",
        "control_plane_api": {
            "id": "abcdefghij",
            "confirmed": True,
            "deployed_stages": [{"stage": "v1"}],
        },
        "lambda_link": {
            "function": "openclaw-api",
            "serving_qualifier": "live",
            "aliases": [{"alias": "live", "version": "7"}],
        },
    }


def test_prepare_compiles_three_target_bound_kits_without_review(tmp_path):
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    output = tmp_path / "build"
    env = os.environ.copy()
    env["OC_PATCH_BUILD_ROOT"] = str(output)
    env["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        ["bash", str(PREPARE), "us-east-1", str(env_path), "--skip-review"],
        cwd=PATCH,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    kits = output / "kits"
    names = [
        "114-tenant-stats-table",
        "114-api-lambda",
        "114-tenants-stats-route",
    ]
    for name in names:
        kit = kits / name
        assert (kit / "CLAUDE.md").is_file()
        assert (kit / "APPLY-INSTRUCTIONS.md").is_file()
        assert (kit / "runtime" / "scripts" / "patch-set.sh").is_file()
        manifest = json.loads((kit / "manifest.json").read_text())
        assert manifest["kit_files"]
    route = json.loads(
        (kits / "114-tenants-stats-route" / "manifest.json").read_text()
    )["api_routes"][0]
    assert route["target_account"] == "111111" + "111111"
    assert route["target_region"] == "us-east-1"
    assert route["api_id"] == "abcdefghij"
    assert route["stage"] == "v1"


def test_prepare_runs_fresh_claude_review_and_seals_each_kit(tmp_path):
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    output = tmp_path / "build"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import re, sys\n"
        "prompt = sys.stdin.read()\n"
        "fingerprint = re.search("
        "r'KIT_FINGERPRINT=([0-9a-f]{64})', prompt).group(1)\n"
        "print('fixture independent review')\n"
        "print('KIT_REVIEW_VERDICT: PASS SCORE=7.0 BLOCKERS=0 "
        "FINGERPRINT=' + fingerprint)\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    env = os.environ.copy()
    env["OC_PATCH_BUILD_ROOT"] = str(output)
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(PREPARE), "us-east-1", str(env_path)],
        cwd=PATCH,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    for kit in (output / "kits").iterdir():
        receipt = json.loads((kit / "REVIEW.json").read_text())
        assert receipt["reviewer"] == "Claude Code safe-mode review-kit.sh"
        assert receipt["score"] == 7.0
        assert (kit / "CLAUDE-REVIEW.txt").is_file()


def test_materializer_refuses_unconfirmed_api(tmp_path):
    value = environment()
    value["control_plane_api"]["confirmed"] = False
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(value), encoding="utf-8")
    result = subprocess.run(
        [
            "python3",
            str(PATCH / "factory" / "scripts" / "materialize-patch.py"),
            str(env_path),
            str(PATCH / "factory" / "manifests" / "114-api-lambda.json"),
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not machine-confirmed" in result.stderr


def test_materializer_refuses_ambiguous_stage(tmp_path):
    value = environment()
    value["control_plane_api"]["deployed_stages"] = [
        {"stage": "blue"},
        {"stage": "green"},
    ]
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(value), encoding="utf-8")
    result = subprocess.run(
        [
            "python3",
            str(PATCH / "factory" / "scripts" / "materialize-patch.py"),
            str(env_path),
            str(PATCH / "factory" / "manifests" / "114-api-lambda.json"),
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "stage is ambiguous" in result.stderr


def test_prepare_refuses_environment_from_another_region(tmp_path):
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    env = os.environ.copy()
    env["OC_PATCH_BUILD_ROOT"] = str(tmp_path / "build")
    env["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        [
            "bash",
            str(PREPARE),
            "eu-west-1",
            str(env_path),
            "--skip-review",
        ],
        cwd=PATCH,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3
    assert "does not match" in result.stderr
