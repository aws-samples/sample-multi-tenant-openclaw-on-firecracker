# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generation-time tests for the control-plane (Lambda) lane.

Every assertion here corresponds to a defect that was found by RUNNING the lane on a live
function, not by reading it. They are cheap and platform-independent because they compile and
inspect text; the live-function behaviour is covered separately by real-machine runs.
"""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


PATCH = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compile_lambda", PATCH / "factory" / "scripts" / "_compile_lambda.py"
)
compiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compiler)

PKG = "deploy/lambda/api"
CHANGED = ["core/auth.py", "services/tenant_service.py", "handler.py"]
UNCHANGED = ["core/kept_one.py", "core/kept_two.py", "services/kept_three.py"]
ACCOUNT = "111111111111"
REGION = "ap-southeast-1"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _fixture(tmp_path, mutate=None):
    repo = tmp_path / "repo"
    (repo / PKG).mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel in CHANGED + UNCHANGED + ["requirements.txt"]:
        path = repo / PKG / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# base {rel}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for rel in CHANGED:
        (repo / PKG / rel).write_text(f"# patched {rel}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "patch")
    patch = _git(repo, "rev-parse", "HEAD")

    def blob(ref, rel):
        return subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{PKG}/{rel}"], capture_output=True
        ).stdout

    kit = tmp_path / "kit"
    kit.mkdir()
    manifest = {
        "id": "fixture-lambda",
        "base_sha": base,
        "patch_sha": patch,
        "status": "READY",
        "kit_files": {},
        "paths": {
            f"{PKG}/{rel}": {
                "change": "M",
                "layer": "C-lambda",
                "artifact_status": "SHIPPED",
                "base_sha256": hashlib.sha256(blob(base, rel)).hexdigest(),
                "patch_sha256": hashlib.sha256(blob(patch, rel)).hexdigest(),
                "operations": [{"class": "AUTO_CLI"}],
            }
            for rel in CHANGED
        },
        "lambda_functions": [
            {
                "function_name": "fixture-fn",
                "package_root": PKG,
                "alias": "live",
                "verify_payload": {"httpMethod": "GET", "resource": "/probe"},
                "verify_expect": {"statusCode": 404},
                "target_account": ACCOUNT,
                "target_region": REGION,
            }
        ],
        "fixes": [],
        "verifications": [],
    }
    if mutate:
        mutate(manifest, repo, patch)
    (kit / "manifest.json").write_text(json.dumps(manifest))
    return kit, repo


def _compiled(tmp_path):
    kit, repo = _fixture(tmp_path)
    result = compiler.compile_lambda_kit(str(kit), str(repo))
    return kit, result


def test_compiles_three_stages(tmp_path):
    kit, result = _compiled(tmp_path)
    assert result["source_count"] == len(CHANGED)
    for name in ("apply.sh", "verify.sh", "rollback.sh", "lambda-state.py"):
        assert (kit / "lib" / "compiled" / result["resource_id"] / name).exists()
    for name in (
        "overlay.json",
        "verify-payload.json",
        "verify-expect.json",
        "rollback-verify-payload.json",
        "rollback-verify-expect.json",
        "lambda-config.json",
    ):
        assert (
            kit / "lib" / "compiled" / result["resource_id"] / "payload" / name
        ).exists()


def test_overlay_deletes_nothing(tmp_path):
    """The overlay used to delete whole first-party dirs and write back only changed files,
    which removed every unchanged module in them. It must replace exact files only."""
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    apply_text = (compiled / "apply.sh").read_text()
    helper = (compiled / "lambda-state.py").read_text()
    assert "deleted none" in helper
    assert "rm -rf \"$work/pkg/$d\"" not in apply_text
    assert "FIRST_PARTY" not in apply_text


def test_payload_travels_as_a_file_not_in_the_environment(tmp_path):
    """A `VAR=<base64> python3` prefix exceeded the exec argv limit with ten sources
    ("Argument list too long", exit 126 on the real host)."""
    kit, result = _compiled(tmp_path)
    apply_text = (kit / "lib" / "compiled" / result["resource_id"] / "apply.sh").read_text()
    assert "SOURCES_B64=" not in apply_text
    assert "OVERLAY_PAYLOAD_FILE=" in apply_text
    assert '"$OVERLAY_PAYLOAD_FILE"' in apply_text


def test_no_invalid_publish_flag(tmp_path):
    """`--publish` is a boolean switch; the CLI rejects `--publish false` as
    "Unknown options: false"."""
    kit, result = _compiled(tmp_path)
    for name in ("apply.sh", "rollback.sh"):
        text = (kit / "lib" / "compiled" / result["resource_id"] / name).read_text()
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "--publish false" not in code


def test_completion_marker_is_written_last(tmp_path):
    """A run interrupted between update-function-code and the alias move must not verify OK."""
    kit, result = _compiled(tmp_path)
    d = kit / "lib" / "compiled" / result["resource_id"]
    apply_text = (d / "apply.sh").read_text()
    marker = apply_text.index('write_marker "$STATE_DIR/complete"')
    alias_move = apply_text.index("update-alias")
    assert marker > alias_move, "the marker must come after the alias move"
    verify_text = (d / "verify.sh").read_text()
    assert 'STATE_DIR/complete' in verify_text
    assert "exit 44" in verify_text


def test_verify_asserts_the_response_not_only_that_it_loaded(tmp_path):
    """FunctionError=None alone passes a handler that returns a 500 or never reached the
    patched modules."""
    kit, result = _compiled(tmp_path)
    verify_text = (kit / "lib" / "compiled" / result["resource_id"] / "verify.sh").read_text()
    assert "VERIFY_EXPECT_FILE=" in verify_text
    assert 'python3 - "$out" "$expect" "$comparison"' in verify_text
    assert "response assertion failed" in verify_text


def test_esm_probe_does_not_use_the_blind_function_name_filter(tmp_path):
    """`list-event-source-mappings --function-name <fn>` returns nothing for an ESM bound to
    <fn>:<alias>, so it reports "no async consumers" for a function that has them."""
    kit, result = _compiled(tmp_path)
    verify_text = (kit / "lib" / "compiled" / result["resource_id"] / "verify.sh").read_text()
    assert "list-event-source-mappings" in verify_text
    esm = verify_text.split("esm_targets()")[1].split("\n}")[0]
    assert "--function-name" not in esm, esm
    assert "starts_with(FunctionArn" in esm, esm


def test_requirements_change_is_refused(tmp_path):
    """The overlay reuses the customer's installed deps, so it cannot honour a dependency
    change — that must fail generation rather than ship a silently wrong package."""

    def mutate(manifest, repo, patch):
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{patch}:{PKG}/requirements.txt"],
            capture_output=True,
        ).stdout
        manifest["paths"][f"{PKG}/requirements.txt"] = {
            "change": "M",
            "layer": "C-lambda",
            "artifact_status": "SHIPPED",
            "base_sha256": None,
            "patch_sha256": hashlib.sha256(blob).hexdigest(),
            "operations": [{"class": "AUTO_CLI"}],
        }

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="dependency change"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_exactly_one_function_per_kit(tmp_path):
    def mutate(manifest, repo, patch):
        manifest["lambda_functions"].append(
            {"function_name": "second-fn", "package_root": PKG, "verify_payload": {}}
        )

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="exactly one"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_shipped_blob_must_match_the_declared_hash(tmp_path):
    """A mis-packaged kit must be caught at generation, not after it is deployed."""

    def mutate(manifest, repo, patch):
        manifest["paths"][f"{PKG}/{CHANGED[0]}"]["patch_sha256"] = "0" * 64

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="does not match declared"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_sources_are_shipped_verbatim_in_one_hash_bound_payload(tmp_path):
    """The patched bytes travel with the kit once, so the target needs no repo access."""
    kit, result = _compiled(tmp_path)
    payload_path = (
        kit
        / "lib"
        / "compiled"
        / result["resource_id"]
        / "payload"
        / "overlay.json"
    )
    payload = json.loads(payload_path.read_text())
    sources = payload["sources"]
    assert set(sources) == set(CHANGED)
    assert base64.b64decode(sources["handler.py"]).decode() == "# patched handler.py\n"
    manifest = json.loads((kit / "manifest.json").read_text())
    rel = payload_path.relative_to(kit).as_posix()
    assert rel in manifest["kit_files"]


def _with_cdk_source(manifest, repo, patch):
    """353 really does change deploy/stacks/lambdas.py, which owns the dispatch ESM binding."""
    manifest["paths"]["deploy/stacks/lambdas.py"] = {
        "change": "M",
        "layer": "D-cdk",
        "artifact_status": "NOT_SHIPPED",
        "base_sha256": None,
        "patch_sha256": None,
        "operations": [{"class": "MANUAL_CLI_REVIEW"}],
    }


def test_esm_binding_conflict_must_be_declared(tmp_path):
    """`api_fn.add_event_source_mapping(...)` renders an UNQUALIFIED ARN, so the async consumer
    runs $LATEST. Repointing it to the alias is invisible to the template and a later
    cdk deploy reverts it. That conflict must surface at GENERATION time, not mid-rollout when
    a lease is already held and an anchor already published."""
    kit, repo = _fixture(tmp_path, _with_cdk_source)
    with pytest.raises(SystemExit, match="event-source-mapping binding"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_declaring_no_repoint_satisfies_the_gate(tmp_path):
    def mutate(manifest, repo, patch):
        _with_cdk_source(manifest, repo, patch)
        manifest["lambda_functions"][0]["esm_binding_conflict"] = "LEAVES_BINDING_UNCHANGED"

    kit, repo = _fixture(tmp_path, mutate)
    assert compiler.compile_lambda_kit(str(kit), str(repo))["source_count"] == len(CHANGED)


def test_a_repoint_must_name_its_template_follow_up(tmp_path):
    """Declaring a repoint without saying how the template catches up leaves the hot change
    silently temporary."""
    def mutate(manifest, repo, patch):
        _with_cdk_source(manifest, repo, patch)
        manifest["lambda_functions"][0]["esm_binding_conflict"] = "REQUIRES_TEMPLATE_FOLLOW_UP"

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="esm_binding_follow_up"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_verify_reports_which_qualifier_each_esm_consumes(tmp_path):
    """The async and sync paths are gated separately; verify has to say which is which."""
    kit, result = _compiled(tmp_path)
    verify_text = (kit / "lib" / "compiled" / result["resource_id"] / "verify.sh").read_text()
    assert "ESM_TARGET" in verify_text
    assert "NOT alias-gated" in verify_text
    assert "follows alias" in verify_text


def test_state_is_scoped_by_account_and_region(tmp_path):
    """Sharing state across environments means a rollback in one could restore the other's
    code from its backup.zip."""
    kit, result = _compiled(tmp_path)
    apply_text = (kit / "lib" / "compiled" / result["resource_id"] / "apply.sh").read_text()
    assert "${STATE_ROOT}/${ACCOUNT_ID}/${REGION}/" in apply_text


def test_target_account_and_region_are_required_and_validated(tmp_path):
    for coordinate in ("target_account", "target_region"):
        def missing(manifest, repo, patch, coordinate=coordinate):
            manifest["lambda_functions"][0].pop(coordinate)

        kit, repo = _fixture(tmp_path / f"missing-{coordinate}", missing)
        with pytest.raises(SystemExit, match=f"{coordinate} is required"):
            compiler.compile_lambda_kit(str(kit), str(repo))

    for account in ("12345", "12345678901x", 123456789012):
        def bad_account(manifest, repo, patch, account=account):
            manifest["lambda_functions"][0]["target_account"] = account

        kit, repo = _fixture(tmp_path / f"account-{account}", bad_account)
        with pytest.raises(SystemExit, match="12-digit account id"):
            compiler.compile_lambda_kit(str(kit), str(repo))

    for index, region in enumerate(("", "us_east_1", "us-east", "us-east-1/evil")):
        def bad_region(manifest, repo, patch, region=region):
            manifest["lambda_functions"][0]["target_region"] = region

        kit, repo = _fixture(tmp_path / f"region-{index}", bad_region)
        with pytest.raises(SystemExit, match="valid AWS region"):
            compiler.compile_lambda_kit(str(kit), str(repo))


def test_every_stage_pins_runtime_region_and_always_uses_sts(tmp_path):
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    for stage in ("apply.sh", "verify.sh", "rollback.sh"):
        script = (compiled / stage).read_text()
        assert f"TARGET_ACCOUNT={ACCOUNT}" in script
        assert f"TARGET_REGION={REGION}" in script
        assert '"$REGION" == "$TARGET_REGION"' in script
        assert "aws sts get-caller-identity" in script
        assert "OC_PATCH_ACCOUNT:-$(aws sts" not in script
        assert '"$STS_ACCOUNT" == "$TARGET_ACCOUNT"' in script
        assert "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true" in script


def test_live_zip_and_overlay_use_the_safe_python_helper(tmp_path):
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    apply = (compiled / "apply.sh").read_text()
    helper = (compiled / "lambda-state.py").read_text()
    assert 'safe-extract "$work/live.zip" "$work/pkg"' in apply
    assert 'apply-overlay "$work/pkg" "$OVERLAY_PAYLOAD_FILE"' in apply
    assert "unzip" not in apply
    for rejection in (
        "absolute path",
        "parent traversal",
        "backslash",
        "symlink",
        "special file",
        "duplicate path",
        "parent/child conflict",
    ):
        assert rejection in helper
    assert "O_NOFOLLOW" in helper


def test_state_is_scoped_by_fixed_review_fingerprint(tmp_path):
    kit, result = _compiled(tmp_path)
    apply_text = (kit / "lib" / "compiled" / result["resource_id"] / "apply.sh").read_text()
    assert 'REVIEW_RECEIPT="$SCRIPT_DIR/../../../REVIEW.json"' in apply_text
    assert "${CONTENT_VERSION}/${KIT_FINGERPRINT}/${RESOURCE_ID}" in apply_text
    assert "OC_PATCH_FINGERPRINT" not in apply_text


def test_non_ready_manifest_is_refused(tmp_path):
    """The host compiler has this gate; the Lambda lane was missing it, so a MANUAL_REVIEW kit
    could compile into an executable recipe."""
    def mutate(manifest, repo, patch):
        manifest["status"] = "MANUAL_REVIEW"

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="not READY"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_non_auto_cli_operation_is_refused(tmp_path):
    def mutate(manifest, repo, patch):
        manifest["paths"][f"{PKG}/{CHANGED[0]}"]["operations"] = [
            {"class": "MANUAL_CLI_REVIEW"}
        ]

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="not AUTO_CLI"):
        compiler.compile_lambda_kit(str(kit), str(repo))


@pytest.mark.parametrize("change", ["D", "R"])
def test_delete_and_rename_are_refused(tmp_path, change):
    """The overlay only adds and replaces. A deleted module would stay in the package, so the
    patched code would run alongside the module it was meant to remove."""
    def mutate(manifest, repo, patch):
        manifest["paths"][f"{PKG}/{CHANGED[0]}"]["change"] = change

    case = tmp_path / change
    case.mkdir()
    kit, repo = _fixture(case, mutate)
    with pytest.raises(SystemExit, match="cannot delete or rename"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_overlay_verifies_the_live_baseline_first(tmp_path):
    """Overwriting a file the customer edited by hand is the same clobbering failure this skill
    exists to prevent, one layer down. An already-patched file must still pass, or a rerun
    would refuse to converge."""
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    payload = json.loads((compiled / "payload" / "overlay.json").read_text())
    helper = (compiled / "lambda-state.py").read_text()
    assert payload["base_hashes"]
    assert payload["patch_hashes"]
    assert "neither the base this " in helper
    assert "patch was built against" in helper
    assert "got not in (want, patched.get(rel))" in helper


def test_compiles_environment_and_exact_table_policy_contract(tmp_path):
    def mutate(manifest, repo, patch):
        function = manifest["lambda_functions"][0]
        function["environment_updates"] = {"FIXED_MODE": "enabled"}
        function["generated_environment"] = {"SIGNING_SECRET": "random_base64_32"}
        function["iam_read_tables"] = ["tenant-table", "job-table"]

    kit, repo = _fixture(tmp_path, mutate)
    result = compiler.compile_lambda_kit(str(kit), str(repo))
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    config = json.loads((compiled / "payload" / "lambda-config.json").read_text())
    assert config == {
        "environment_updates": {"FIXED_MODE": "enabled"},
        "generated_environment": {"SIGNING_SECRET": "random_base64_32"},
        "iam_read_tables": ["tenant-table", "job-table"],
    }
    apply_text = (compiled / "apply.sh").read_text()
    assert "update-function-configuration" in apply_text
    assert '--environment "file://$STATE_DIR/merged-environment.json"' in apply_text
    assert '--revision-id "$rev"' in apply_text
    assert "aws lambda wait function-updated" in apply_text
    assert "put-role-policy" in apply_text
    helper = (compiled / "lambda-state.py").read_text()
    for action in (
        "dynamodb:DescribeTable",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchGetItem",
    ):
        assert action in helper
    assert 'arn + "/index/*"' in helper


def test_compiles_distinct_rollback_probe_contract(tmp_path):
    rollback_payload = {"httpMethod": "GET", "resource": "/old-contract"}
    rollback_expect = {"statusCode": 404}

    def mutate(manifest, repo, patch):
        function = manifest["lambda_functions"][0]
        function["rollback_verify_payload"] = rollback_payload
        function["rollback_verify_expect"] = rollback_expect

    kit, repo = _fixture(tmp_path, mutate)
    result = compiler.compile_lambda_kit(str(kit), str(repo))
    payload = kit / "lib" / "compiled" / result["resource_id"] / "payload"
    assert json.loads((payload / "rollback-verify-payload.json").read_text()) == rollback_payload
    assert json.loads((payload / "rollback-verify-expect.json").read_text()) == rollback_expect
    rollback = (payload.parent / "rollback.sh").read_text()
    assert "ROLLBACK_VERIFY_PAYLOAD_FILE" in rollback
    assert "rollback-expect-latest.json" in rollback
    assert "rollback_invoke_ok '$LATEST'" in rollback
    assert not any(
        line.strip().startswith("invoke_ok '$LATEST'")
        for line in rollback.splitlines()
    )


def test_rollback_probe_declarations_are_a_pair(tmp_path):
    def mutate(manifest, repo, patch):
        manifest["lambda_functions"][0]["rollback_verify_expect"] = {
            "statusCode": 404
        }

    kit, repo = _fixture(tmp_path, mutate)
    with pytest.raises(SystemExit, match="must be declared together"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_environment_declarations_fail_closed_at_generation(tmp_path):
    def overlap(manifest, repo, patch):
        function = manifest["lambda_functions"][0]
        function["environment_updates"] = {"TOKEN": "fixed"}
        function["generated_environment"] = {"TOKEN": "random_base64_32"}

    kit, repo = _fixture(tmp_path / "overlap", overlap)
    with pytest.raises(SystemExit, match="overlap"):
        compiler.compile_lambda_kit(str(kit), str(repo))

    def unsupported(manifest, repo, patch):
        manifest["lambda_functions"][0]["generated_environment"] = {
            "TOKEN": "random_uuid"
        }

    kit, repo = _fixture(tmp_path / "unsupported", unsupported)
    with pytest.raises(SystemExit, match="only supports"):
        compiler.compile_lambda_kit(str(kit), str(repo))


def test_generated_recipes_never_delete_state(tmp_path):
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    for name in ("apply.sh", "verify.sh", "rollback.sh"):
        executable = "\n".join(
            line
            for line in (compiled / name).read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "rm " not in executable
        assert "rm\t" not in executable
        assert 'STATE_DIR/archive' in executable


def test_lambda_recipes_use_only_the_public_exit_code_contract(tmp_path):
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    text = "\n".join(
        (compiled / name).read_text()
        for name in ("apply.sh", "verify.sh", "rollback.sh", "lambda-state.py")
    )
    for code in (47, 48, 50, 51, 52, 53, 54):
        assert f"exit {code}" not in text
        assert f", {code})" not in text
    assert "exit 44" in text
    assert "exit 49" in text


def test_apply_binds_update_and_publish_to_the_patched_zip_hash(tmp_path):
    kit, result = _compiled(tmp_path)
    apply = (
        kit / "lib" / "compiled" / result["resource_id"] / "apply.sh"
    ).read_text()
    assert 'expected_sha="$(file_code_sha "$new_zip")"' in apply
    assert '"$response_sha" == "$expected_sha"' in apply
    assert '"$live" == "$expected_sha"' in apply
    assert 'write_marker "$STATE_DIR/applied.sha256" "$expected_sha"' in apply
    assert '--code-sha256 "$expected_sha"' in apply
    update = apply.index('response_sha="$(aws lambda update-function-code')
    wait = apply.index("aws lambda wait function-updated", update)
    live_read = apply.index('live="$(live_code_sha "$FUNCTION")"', wait)
    claim = apply.index(
        'write_marker "$STATE_DIR/applied.sha256" "$expected_sha"', live_read
    )
    publish = apply.index("aws lambda publish-version", claim)
    assert update < wait < live_read < claim < publish


def test_backup_metadata_is_atomic_create_only_and_hash_bound(tmp_path):
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    apply = (compiled / "apply.sh").read_text()
    helper = (compiled / "lambda-state.py").read_text()
    assert "backup-create" in apply
    assert "os.link(candidate_meta, state / \"backup.meta\")" in helper
    assert "backup_zip_sha256" in helper
    assert "anchor_version" in helper
    assert '"alias"' in helper
    assert '"esm"' in helper
    assert '> "$STATE_DIR/backup.meta"' not in apply


def test_mutating_aws_failures_use_the_shared_classifier(tmp_path):
    kit, result = _compiled(tmp_path)
    compiled = kit / "lib" / "compiled" / result["resource_id"]
    apply = (compiled / "apply.sh").read_text()
    rollback = (compiled / "rollback.sh").read_text()

    for error_name in (
        "put-role-policy.err",
        "publish-version.err",
        "update-alias.err",
    ):
        assert error_name in apply
    for variable in ("error", "publish_error", "alias_error"):
        assert f'exit "$(classify_aws_error "${variable}")"' in apply

    for error_name in (
        "rollback-environment.err",
        "delete-role-policy.err",
        "rollback-alias.err",
    ):
        assert error_name in rollback
    for variable in (
        "rollback_environment_error",
        "delete_policy_error",
        "rollback_alias_error",
    ):
        assert f'exit "$(classify_aws_error "${variable}")"' in rollback
