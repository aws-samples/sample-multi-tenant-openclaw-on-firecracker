# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


PATCH = Path(__file__).resolve().parents[1]
SCRIPTS = PATCH / "factory" / "scripts"
PREPARE = SCRIPTS / "prepare.sh"
DISABLED_EXIT = 78
DISABLED_DETAILS = (
    "PATCH_114_FACTORY_DISABLED",
    "tenant-stats writer Lambda",
    "writer IAM and environment",
    "EventBridge schedule",
    "authenticated HTTP end-to-end test",
    "CUSTOM authorizer",
)

# These are the infrastructure-generating entry points and the two runtime
# drivers that can execute generated infrastructure changes.
FACTORY_ENTRYPOINTS = (
    ("prepare", ("bash", str(PREPARE))),
    ("materialize", ("python3", str(SCRIPTS / "materialize-patch.py"))),
    ("compile", ("bash", str(SCRIPTS / "compile-kit.sh"))),
    ("compile-apigw", ("python3", str(SCRIPTS / "_compile_apigw.py"))),
    ("compile-ddb", ("python3", str(SCRIPTS / "_compile_ddb_create.py"))),
    ("compile-lambda", ("python3", str(SCRIPTS / "_compile_lambda.py"))),
    ("patch-set", ("bash", str(SCRIPTS / "patch-set.sh"))),
    ("autopatch", ("bash", str(SCRIPTS / "autopatch.sh"))),
)


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


def load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(f"patch114_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "command"),
    FACTORY_ENTRYPOINTS,
    ids=[entry[0] for entry in FACTORY_ENTRYPOINTS],
)
def test_every_factory_entrypoint_fails_closed_before_argument_parsing(
    tmp_path, name, command
):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        command,
        cwd=PATCH,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == DISABLED_EXIT, (name, result.stderr)
    details = (
        DISABLED_DETAILS[:1] if name.startswith("compile-") else DISABLED_DETAILS
    )
    for detail in details:
        assert detail in result.stderr, (name, detail, result.stderr)


def test_prepare_fails_without_creating_build_output(tmp_path):
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    output = tmp_path / "build"
    env = os.environ.copy()
    env["OC_PATCH_BUILD_ROOT"] = str(output)
    env["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        ["bash", str(PREPARE), "example-region", str(env_path), "--skip-review"],
        cwd=PATCH,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == DISABLED_EXIT, result.stderr
    assert not output.exists()


def test_materializer_fails_without_creating_kits(tmp_path):
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    output = tmp_path / "kits"

    result = subprocess.run(
        [
            "python3",
            str(SCRIPTS / "materialize-patch.py"),
            str(env_path),
            str(PATCH / "factory" / "manifests" / "114-api-lambda.json"),
            str(output),
        ],
        cwd=PATCH,
        capture_output=True,
        text=True,
    )

    assert result.returncode == DISABLED_EXIT, result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("script", "function", "args"),
    (
        (
            "materialize-patch.py",
            "materialize",
            (Path("environment.json"), Path("lambda.json"), Path("kits"), None),
        ),
        ("_compile_apigw.py", "compile_apigw_kit", ("kit",)),
        ("_compile_ddb_create.py", "compile_ddb_create_kit", ("kit",)),
        ("_compile_lambda.py", "compile_lambda_kit", ("kit", "repo")),
    ),
)
def test_direct_generator_functions_fail_closed(tmp_path, script, function, args):
    module = load_script(script)
    before = sorted(tmp_path.iterdir())

    with pytest.raises(SystemExit) as raised:
        getattr(module, function)(*args)

    assert raised.value.code == DISABLED_EXIT
    assert sorted(tmp_path.iterdir()) == before


def test_compiler_fails_without_modifying_existing_kit(tmp_path):
    kit = tmp_path / "kit"
    kit.mkdir()
    manifest = kit / "manifest.json"
    original = b'{"id":"existing-kit","sentinel":"unchanged"}\n'
    manifest.write_bytes(original)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "compile-kit.sh"), str(kit), str(PATCH.parents[1])],
        cwd=PATCH,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == DISABLED_EXIT, result.stderr
    assert manifest.read_bytes() == original
    assert list(kit.iterdir()) == [manifest]


@pytest.mark.parametrize("driver", ("patch-set.sh", "autopatch.sh"))
def test_runtime_driver_fails_closed_with_apply_arguments(tmp_path, driver):
    kit = tmp_path / "kit"
    kit.mkdir()
    env_path = tmp_path / "environment.json"
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    answers = tmp_path / "answers"
    answers.mkdir()
    command = ["bash", str(SCRIPTS / driver)]
    if driver == "patch-set.sh":
        command.extend(["apply", str(env_path), str(answers), str(kit)])
    else:
        command.extend([str(kit), str(env_path)])

    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == DISABLED_EXIT, result.stderr
    assert "PATCH_114_FACTORY_DISABLED" in result.stderr
    assert sorted(tmp_path.iterdir()) == [answers, env_path, kit]
