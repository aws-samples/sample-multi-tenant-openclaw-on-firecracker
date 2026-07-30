# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path


PATCH = Path(__file__).resolve().parents[1]
SCRIPTS = PATCH / "factory" / "scripts"
LAMBDA_TEMPLATE = PATCH / "factory" / "manifests" / "114-api-lambda.json"
PATCH_SHA = "f8b9e14e5f456a24dc8fc597528a7b1b1540a9f3"
ACCOUNT = "111111111111"
REGION = "us-east-1"
API_ID = "abcdefghij"
STAGE = "v1"
CLIENT_URL = f"https://{API_ID}.execute-api.{REGION}.amazonaws.com/{STAGE}"
HEADERS_SHA = hashlib.sha256(b'{"x-api-key":"secret"}').hexdigest()


def load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(f"patch114_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_facts() -> dict[str, object]:
    return {
        "schema_version": 1,
        "config_sha256": "c" * 64,
        "api_mode": "private",
        "api_mode_source": "api.mode",
        "tenant_stats_enabled": None,
    }


def environment(entrypoint_kind: str = "explicit-rest-resources") -> dict[str, object]:
    return {
        "account": ACCOUNT,
        "region": REGION,
        "control_plane_api": {
            "id": API_ID,
            "stage": STAGE,
            "confirmed": entrypoint_kind == "explicit-rest-resources",
            "configured_client_url": CLIENT_URL,
            "entrypoint_kind": entrypoint_kind,
            "probe_headers_sha256": HEADERS_SHA,
            "probe_results": [
                {"path": "/tenants", "status": 200},
                {"path": "/hosts", "status": 200},
            ],
            "deployed_stages": [{"stage": STAGE}],
            "reference_method": {
                "path": "/tenants",
                "method": "GET",
                "authorization_type": "CUSTOM",
                "api_key_required": True,
                "authorizer_id": "auth123",
                "authorizer_name": "platform-authorizer",
                "authorization_scopes": ["tenants.read"],
                "integration_uri": (
                    f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/"
                    f"functions/arn:aws:lambda:{REGION}:{ACCOUNT}:"
                    "function:openclaw-api:live/invocations"
                ),
            },
        },
        "lambda_link": {
            "function": "openclaw-api",
            "serving_qualifier": "live",
            "aliases": [{"alias": "live", "version": "7"}],
        },
        "tenant_stats_inputs": {
            "tenants_table": "openclaw-tenants",
            "assets_bucket": "customer-assets-bucket",
            "rootfs_prefix": "deployment/rootfs",
        },
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def materialize(tmp_path: Path, env: dict[str, object] | None = None) -> Path:
    environment_path = tmp_path / "environment.json"
    config_path = tmp_path / "config-facts.json"
    output = tmp_path / "kits"
    write_json(environment_path, env or environment())
    write_json(config_path, config_facts())
    result = subprocess.run(
        [
            "python3",
            str(SCRIPTS / "materialize-patch.py"),
            str(environment_path),
            str(config_path),
            str(LAMBDA_TEMPLATE),
            str(output),
        ],
        cwd=PATCH,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output


def test_customer_config_is_hint_only_and_hash_bound(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "api:\n  mode: private\ntenant_stats:\n  enabled: true\n",
        encoding="utf-8",
    )
    output = tmp_path / "facts.json"
    result = subprocess.run(
        ["python3", str(SCRIPTS / "read-customer-config.py"), str(config), str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    facts = json.loads(output.read_text(encoding="utf-8"))
    assert facts["api_mode"] == "private"
    assert facts["tenant_stats_enabled"] is True
    assert facts["config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert "routing hints only" in facts["note"]
    assert API_ID not in json.dumps(facts)


def test_materializer_builds_three_target_bound_kits(tmp_path):
    output = materialize(tmp_path)
    names = {
        "114-tenant-stats-table",
        "114-api-lambda",
        "114-tenants-stats-route",
    }
    assert {path.name for path in output.iterdir()} == names
    manifests = {
        name: json.loads((output / name / "manifest.json").read_text())
        for name in names
    }
    for manifest in manifests.values():
        target = manifest["target_confirmation"]
        assert target["configured_api_mode"] == "private"
        assert target["confirmed_api_id"] == API_ID
        assert target["confirmed_stage"] == STAGE
        assert target["confirmed_client_url"] == CLIENT_URL
        assert target["entrypoint_kind"] == "explicit-rest-resources"
        assert target["proxy_resources_are_not_targets"] is True
        assert target["reference_authorization_type"] == "CUSTOM"
        assert target["reference_authorizer_name"] == "platform-authorizer"
        assert target["reference_api_key_required"] is True
        assert target["reference_authorization_scopes"] == ["tenants.read"]
        assert target["authenticated_probe_headers_sha256"] == HEADERS_SHA

    route = manifests["114-tenants-stats-route"]["api_routes"][0]
    assert route["authorization_type"] == "CUSTOM"
    assert route["api_key_required"] is True
    assert route["authorizer_id"] == "auth123"
    assert route["authorizer_name"] == "platform-authorizer"
    assert route["authorization_scopes"] == ["tenants.read"]
    assert route["invoke_url"] == CLIENT_URL
    assert route["probe"]["headers_file_sha256"] == HEADERS_SHA
    assert route["probe"]["expected_body_fields"] == {
        "business": {},
        "snapshot_stale": False,
    }

    function = manifests["114-api-lambda"]["lambda_functions"][0]
    assert function["target_account"] == ACCOUNT
    assert function["target_region"] == REGION

    backend = manifests["114-tenant-stats-table"]["tenant_stats_backends"][0]
    assert backend["table"]["name"] == "openclaw-tenant-stats"
    assert backend["writer"]["environment"]["TENANTS_TABLE"] == "openclaw-tenants"
    rollback = manifests["114-tenant-stats-table"]["rollback_notice"]
    assert "disables the patch-owned EventBridge schedule" in rollback["behavior"]
    assert "DynamoDB table and its data" in rollback["retained_resources"]


def test_proxy_or_unconfirmed_api_is_never_materialized(tmp_path):
    module = load_script("materialize-patch.py")
    for index, kind in enumerate(("proxy-resource", "unresolved")):
        case = tmp_path / str(index)
        case.mkdir()
        env = environment(kind)
        if kind == "proxy-resource":
            env["control_plane_api"]["confirmed"] = True
        env_path = case / "environment.json"
        facts_path = case / "config-facts.json"
        write_json(env_path, env)
        write_json(facts_path, config_facts())
        output = case / "kits"
        try:
            module.materialize(
                env_path,
                facts_path,
                LAMBDA_TEMPLATE,
                output,
                None,
            )
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError(f"{kind} unexpectedly materialized")
        assert (
            "not machine-confirmed" in message
            or "ANY /{proxy+} is not an accepted target" in message
        )
        assert not output.exists()


def test_entrypoint_confirmation_is_required_and_tamper_checked(tmp_path):
    output = materialize(tmp_path)
    kit = output / "114-api-lambda"
    interview = load_script("interview-once.py")
    manifest = json.loads((kit / "manifest.json").read_text())
    questions = interview.questions(manifest)
    target = next(item for item in questions if item["id"] == "Q-api-entrypoint")
    assert "Confirm 100%" in target["ask"]
    assert "ANY /{proxy+} resources are invalid" in target["ask"]
    assert "authorization_type=CUSTOM" in target["ask"]
    assert "authorizer=platform-authorizer" in target["ask"]
    assert "api_key_required=True" in target["ask"]
    assert "scopes=['tenants.read']" in target["ask"]

    answers = {}
    for question in questions:
        if question["id"] == "Q-fleet-widening":
            answers[question["id"]] = "hold"
        elif question["expects"] == "yes/no":
            answers[question["id"]] = "yes"
        else:
            answers[question["id"]] = "defer"
    answers_path = tmp_path / "answers.json"
    write_json(answers_path, answers)
    assert interview.cmd_record(str(kit), str(answers_path)) == 0
    assert interview.cmd_check(str(kit)) == 0

    decision_path = kit / "DECISION.json"
    decision = json.loads(decision_path.read_text())
    decision["answers"]["Q-api-entrypoint"] = "no"
    write_json(decision_path, decision)
    assert interview.cmd_check(str(kit)) == 5


def test_backend_interview_names_resources_retained_by_rollback(tmp_path):
    output = materialize(tmp_path)
    manifest = json.loads(
        (output / "114-tenant-stats-table" / "manifest.json").read_text()
    )
    interview = load_script("interview-once.py")
    rollback = next(
        item for item in interview.questions(manifest) if item["id"] == "Q-rollback"
    )
    assert "disables the patch-owned EventBridge schedule" in rollback["ask"]
    assert "DynamoDB table and its data" in rollback["ask"]
    assert "writer IAM role and inline policy" in rollback["ask"]


def test_discovery_contract_has_fixed_authenticated_paths_and_proxy_rejection():
    script = (SCRIPTS / "discover-env.sh").read_text(encoding="utf-8")
    for variable in (
        "OC_CONTROL_PLANE_API_ID",
        "OC_CONTROL_PLANE_STAGE",
        "OC_CONTROL_PLANE_URL",
        "OC_CONTROL_PLANE_PROBE_HEADERS_FILE",
    ):
        assert variable in script
    assert 'probe_paths <<< "/tenants,/hosts"' in script
    assert "ANY /{proxy+} is never a target for this patch" in script
    assert "OC_CONTROL_PLANE_PROBE_PATHS:-" not in script
    assert PATCH_SHA in (
        PATCH / "factory" / "manifests" / "114-api-lambda.json"
    ).read_text()


def test_prepare_detects_backslash_space_and_requires_modern_bash(tmp_path):
    malformed = tmp_path / "apply.sh"
    malformed.write_bytes(b"aws apigateway put-method \\\\ \n  --rest-api-id x\n")
    result = subprocess.run(
        ["grep", "-R", "-F", "-l", "\\ ", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert str(malformed) in result.stdout

    prepare = (SCRIPTS / "prepare.sh").read_text(encoding="utf-8")
    autopatch = (SCRIPTS / "autopatch.sh").read_text(encoding="utf-8")
    assert "grep -R -F -l $'\\\\ '" in prepare
    assert "BASH_VERSINFO[0]" in prepare
    assert "BASH_VERSINFO[0]" in autopatch


def test_preflight_hash_binds_the_operator_confirmed_probe_headers():
    preflight = (SCRIPTS / "preflight-once.sh").read_text(encoding="utf-8")
    assert 'HEADERS_PATH="${OC_PATCH_HTTP_HEADERS_FILE:-}"' in preflight
    assert ".target_confirmation.authenticated_probe_headers_sha256" in preflight
    assert "probe headers changed after API confirmation" in preflight
