#!/usr/bin/env python3
"""Immutable planning, execution, receipts, resume, verification, and rollback."""

from __future__ import annotations

from pathlib import Path

from _patch_context import (
    RUNNER_VERSION,
    assert_target_identity,
    command_context,
    command_state,
    execution_context_inventory,
    redacted_resources,
    run_command,
    runner_fingerprint,
    validate_backup_metadata,
)
from _patch_model import (
    JsonObject,
    PatchError,
    file_sha256,
    fingerprint,
    load_catalog,
    load_json,
    selected_operation_order,
    validate_artifacts,
    validate_environment,
    validate_manifest,
    write_json_atomic,
)
from _patch_store import (
    COMPLETE_STATUSES,
    ROLLBACK_STATUSES,
    RunLock,
    acceptance_receipt_path,
    load_receipt,
    operation_receipt_path,
    save_receipt,
    seal,
    utc_now,
    validate_seal,
)


def _plan_core(plan: JsonObject) -> JsonObject:
    return {key: value for key, value in plan.items() if key != "plan_fingerprint"}


def _validate_plan_file(plan: JsonObject) -> None:
    expected = plan.get("plan_fingerprint")
    if not isinstance(expected, str) or fingerprint(_plan_core(plan)) != expected:
        raise PatchError("PLAN.json fingerprint is invalid")


def _input_fingerprints(
    manifest: JsonObject,
    environment: JsonObject,
    catalog: JsonObject,
    execution_context: JsonObject,
) -> JsonObject:
    return {
        "manifest": fingerprint(manifest),
        "environment": fingerprint(environment),
        "catalog": fingerprint(
            {
                "schema_version": catalog["schema_version"],
                "contracts": list(catalog["contracts"].values()),
            }
        ),
        "runner": runner_fingerprint(),
        "execution_context": fingerprint(execution_context),
    }


def _customer_baseline_binding(
    environment: JsonObject,
    environment_path: Path,
) -> JsonObject | None:
    baseline = environment.get("baseline")
    if baseline is None:
        return None
    declared = Path(baseline["snapshot"])
    snapshot = (
        declared
        if declared.is_absolute()
        else environment_path.parent / declared
    )
    if snapshot.is_symlink():
        raise PatchError("customer baseline snapshot must not be a symlink")
    snapshot = snapshot.resolve(strict=True)
    if not snapshot.is_file():
        raise PatchError("customer baseline snapshot must be a regular file")
    actual_sha256 = file_sha256(snapshot)
    if actual_sha256 != baseline["sha256"]:
        raise PatchError("customer baseline snapshot SHA-256 does not match")
    return {
        "authority": baseline["authority"],
        "snapshot": str(snapshot),
        "sha256": actual_sha256,
        "preserve_unowned": True,
        **(
            {"captured_at": baseline["captured_at"]}
            if "captured_at" in baseline
            else {}
        ),
    }


def _assert_provenance_target_binding(
    manifest: JsonObject,
    environment_path: Path,
) -> None:
    provenance = manifest.get("provenance")
    if provenance is None:
        return
    expected = manifest.get("execution_profile_sha256") or provenance.get(
        "target", {}
    ).get("profile_sha256")
    if expected != file_sha256(environment_path):
        raise PatchError(
            "target profile does not match the manifest provenance binding"
        )


def _assert_write_policy(environment: JsonObject) -> None:
    policy = environment.get("policy")
    if policy is not None and policy["write_mode"] == "QUALIFY_ONLY":
        raise PatchError("environment policy QUALIFY_ONLY forbids writes")


def _invalidate_delivery(run_dir: Path) -> None:
    (run_dir / "DELIVERY.json").unlink(missing_ok=True)
    (run_dir / "VERIFY.json").unlink(missing_ok=True)


def create_plan(
    manifest_path: Path,
    environment_path: Path,
    catalog_path: Path,
    run_dir: Path,
) -> JsonObject:
    manifest_path = manifest_path.resolve(strict=True)
    environment_path = environment_path.resolve(strict=True)
    catalog_path = catalog_path.resolve(strict=True)
    run_dir = run_dir.resolve()
    kit_dir = manifest_path.parent

    manifest = load_json(manifest_path)
    environment = load_json(environment_path)
    catalog = load_catalog(catalog_path)
    validate_environment(environment)
    _assert_provenance_target_binding(manifest, environment_path)
    baseline_binding = _customer_baseline_binding(environment, environment_path)
    model = validate_manifest(manifest, catalog)
    validate_artifacts(manifest, kit_dir)
    execution_context = execution_context_inventory(
        manifest,
        environment,
        model,
        kit_dir,
        run_dir,
    )

    operation_order = selected_operation_order(model)
    blocked = [
        operation_id
        for operation_id in operation_order
        if model["operations"][operation_id]["approval"] == "BLOCKED"
    ]
    if blocked:
        raise PatchError(f"selected operations are BLOCKED: {', '.join(blocked)}")

    context = command_context(manifest, environment, kit_dir, run_dir)
    observed_target = assert_target_identity(
        manifest,
        environment,
        context,
        kit_dir,
    )
    planned_operations = []
    for operation_id in operation_order:
        operation = model["operations"][operation_id]
        check = operation["phases"]["check"]
        assert_target_identity(
            manifest,
            environment,
            context,
            kit_dir,
            check,
        )
        state = command_state(
            check,
            run_command(check, context, kit_dir, read_only=True),
        )
        if state in {"DRIFT", "UNKNOWN"}:
            raise PatchError(f"operation {operation_id} check returned {state}")
        planned_operations.append(
            {
                "id": operation_id,
                "summary": operation["summary"],
                "features": operation["features"],
                "provides": operation["provides"],
                "artifacts": operation["artifacts"],
                "source_changes": operation["source_changes"],
                "execution": operation["execution"],
                "approval": operation["approval"],
                "rollback": operation["rollback"],
                "idempotent": operation["idempotent"],
                "depends_on": operation["depends_on"],
                "observed_state": state,
                "phases": operation["phases"],
                "manual": operation.get("manual"),
            }
        )

    selected_acceptance = [
        check
        for check in model["acceptance"].values()
        if model["enabled_features"].intersection(check["features"])
    ]
    plan: JsonObject = {
        "schema_version": 1,
        "runner_version": RUNNER_VERSION,
        "created_at": utc_now(),
        "paths": {
            "manifest": str(manifest_path),
            "environment": str(environment_path),
            "catalog": str(catalog_path),
            "kit_dir": str(kit_dir),
            "run_dir": str(run_dir),
        },
        "input_fingerprints": _input_fingerprints(
            manifest,
            environment,
            catalog,
            execution_context,
        ),
        "execution_context": execution_context,
        "artifact_inventory": manifest["artifacts"],
        "patch": manifest["patch"],
        "target": {
            **observed_target,
            "resources": redacted_resources(environment["resources"]),
            **(
                {"baseline": baseline_binding}
                if baseline_binding is not None
                else {}
            ),
            **(
                {"policy": environment["policy"]}
                if "policy" in environment
                else {}
            ),
        },
        "enabled_features": sorted(model["enabled_features"]),
        "operations": planned_operations,
        "acceptance_checks": selected_acceptance,
    }
    plan["plan_fingerprint"] = fingerprint(plan)

    with RunLock(run_dir):
        existing_path = run_dir / "PLAN.json"
        if existing_path.exists():
            existing = load_json(existing_path)
            if existing.get("plan_fingerprint") != plan["plan_fingerprint"]:
                receipts = run_dir / "receipts"
                if receipts.exists() and any(receipts.rglob("*.json")):
                    raise PatchError("run directory has receipts for a different plan")
                (run_dir / "APPROVAL.json").unlink(missing_ok=True)
        write_json_atomic(existing_path, plan)
    return plan


def _load_bound_inputs(
    run_dir: Path,
) -> tuple[JsonObject, JsonObject, JsonObject, JsonObject, JsonObject, Path]:
    run_dir = run_dir.resolve()
    plan = load_json(run_dir / "PLAN.json")
    _validate_plan_file(plan)
    paths = plan["paths"]
    if paths.get("run_dir") != str(run_dir):
        raise PatchError("plan is bound to a different run directory")
    manifest = load_json(Path(paths["manifest"]))
    environment = load_json(Path(paths["environment"]))
    catalog = load_catalog(Path(paths["catalog"]))
    kit_dir = Path(paths["kit_dir"])
    validate_environment(environment)
    _assert_provenance_target_binding(
        manifest,
        Path(paths["environment"]),
    )
    baseline_binding = _customer_baseline_binding(
        environment,
        Path(paths["environment"]),
    )
    if baseline_binding != plan.get("target", {}).get("baseline"):
        raise PatchError(
            "customer baseline binding changed; create and approve a new plan"
        )
    model = validate_manifest(manifest, catalog)
    validate_artifacts(manifest, kit_dir)
    execution_context = execution_context_inventory(
        manifest,
        environment,
        model,
        kit_dir,
        run_dir,
    )
    if execution_context != plan.get("execution_context"):
        raise PatchError("execution context changed; create and approve a new plan")
    current = _input_fingerprints(
        manifest,
        environment,
        catalog,
        execution_context,
    )
    if current != plan["input_fingerprints"]:
        raise PatchError("plan inputs changed; create and approve a new plan")
    if manifest["artifacts"] != plan["artifact_inventory"]:
        raise PatchError("artifact inventory changed; create and approve a new plan")
    return plan, manifest, environment, catalog, model, kit_dir


def approve_plan(run_dir: Path, *, allow_irreversible: bool) -> JsonObject:
    run_dir = run_dir.resolve()
    with RunLock(run_dir):
        plan, _, _, _, _, _ = _load_bound_inputs(run_dir)
        if (
            plan.get("target", {})
            .get("policy", {})
            .get("write_mode")
            == "QUALIFY_ONLY"
            and any(
                operation["observed_state"] != "TARGET"
                for operation in plan["operations"]
            )
        ):
            raise PatchError(
                "environment policy QUALIFY_ONLY cannot approve pending writes"
            )
        irreversible = [
            operation["id"]
            for operation in plan["operations"]
            if operation["rollback"] == "IRREVERSIBLE"
            and operation["observed_state"] != "TARGET"
        ]
        if irreversible and not allow_irreversible:
            raise PatchError(
                "plan contains IRREVERSIBLE operations; approve again with "
                f"--allow-irreversible: {', '.join(irreversible)}"
            )
        approval = {
            "schema_version": 1,
            "plan_fingerprint": plan["plan_fingerprint"],
            "approved_at": utc_now(),
            "allow_irreversible": allow_irreversible,
        }
        seal(approval, "approval_fingerprint")
        write_json_atomic(run_dir / "APPROVAL.json", approval)
        return approval


def _assert_approved(run_dir: Path, plan: JsonObject) -> JsonObject:
    approval = load_json(run_dir / "APPROVAL.json")
    validate_seal(approval, "approval_fingerprint", "approval")
    if approval.get("plan_fingerprint") != plan["plan_fingerprint"]:
        raise PatchError("approval does not match this plan")
    if not isinstance(approval.get("allow_irreversible"), bool):
        raise PatchError("approval allow_irreversible is invalid")
    return approval


def _run_phase(
    operation: JsonObject,
    phase: str,
    receipt: JsonObject,
    path: Path,
    context: JsonObject,
    kit_dir: Path,
) -> JsonObject:
    result = run_command(
        operation["phases"][phase],
        context,
        kit_dir,
        metadata=phase in {"backup", "backup_verify"},
    )
    receipt["phase_results"][phase] = result
    save_receipt(path, receipt, receipt["status"])
    return result


def _check_operation(
    operation: JsonObject,
    receipt: JsonObject,
    path: Path,
    context: JsonObject,
    kit_dir: Path,
) -> str:
    result = _run_phase(operation, "check", receipt, path, context, kit_dir)
    return command_state(operation["phases"]["check"], result)


def _verify_operation(
    operation: JsonObject,
    receipt: JsonObject,
    path: Path,
    context: JsonObject,
    kit_dir: Path,
    status: str,
) -> None:
    result = _run_phase(operation, "verify", receipt, path, context, kit_dir)
    if result["returncode"] != 0:
        save_receipt(path, receipt, "VERIFY_FAILED")
        raise PatchError(f"operation {operation['id']} verification failed")
    save_receipt(path, receipt, status)


def _verify_backup(
    operation: JsonObject,
    receipt: JsonObject,
    path: Path,
    context: JsonObject,
    kit_dir: Path,
    failure_status: str = "BACKUP_INVALID",
    clear_on_failure: bool = True,
) -> None:
    backup_identity = receipt.get("backup_identity")
    validate_backup_metadata(backup_identity)
    result = _run_phase(
        operation,
        "backup_verify",
        receipt,
        path,
        context,
        kit_dir,
    )
    if result["returncode"] != 0 or result.get("metadata") != backup_identity:
        if clear_on_failure:
            receipt["backup_completed"] = False
        save_receipt(path, receipt, failure_status)
        raise PatchError(f"operation {operation['id']} backup verification failed")


def _execute_compiled(
    operation: JsonObject,
    receipt: JsonObject,
    path: Path,
    context: JsonObject,
    kit_dir: Path,
    approval: JsonObject,
    manifest: JsonObject,
    environment: JsonObject,
) -> None:
    assert_target_identity(
        manifest,
        environment,
        context,
        kit_dir,
        operation["phases"]["check"],
    )
    interrupted = receipt["changed"] and receipt["status"] not in COMPLETE_STATUSES
    state = _check_operation(operation, receipt, path, context, kit_dir)
    receipt["observed_state"] = state
    if state in {"DRIFT", "UNKNOWN"}:
        save_receipt(path, receipt, f"HALTED_{state}")
        raise PatchError(f"operation {operation['id']} live state is {state}")
    if state == "TARGET":
        if interrupted:
            if not operation["idempotent"]:
                save_receipt(path, receipt, "UNKNOWN_AFTER_INTERRUPT")
                raise PatchError(
                    f"operation {operation['id']} was interrupted and is not idempotent"
                )
            _assert_write_policy(environment)
            if operation["rollback"] == "RESTORE":
                _verify_backup(operation, receipt, path, context, kit_dir)
            save_receipt(path, receipt, "RECONCILE_INTENT")
            result = _run_phase(
                operation,
                "apply",
                receipt,
                path,
                context,
                kit_dir,
            )
            if result["returncode"] != 0:
                save_receipt(path, receipt, "RECONCILE_FAILED")
                raise PatchError(
                    f"operation {operation['id']} reconciliation failed"
                )
            save_receipt(path, receipt, "APPLIED")
            state = _check_operation(operation, receipt, path, context, kit_dir)
            receipt["observed_state"] = state
            if state != "TARGET":
                save_receipt(path, receipt, f"POST_RECONCILE_{state}")
                raise PatchError(
                    f"operation {operation['id']} reconciliation reached {state}"
                )
        status = "VERIFIED" if receipt["changed"] or interrupted else "SKIPPED"
        _verify_operation(operation, receipt, path, context, kit_dir, status)
        return

    if interrupted and not operation["idempotent"]:
        save_receipt(path, receipt, "UNKNOWN_AFTER_INTERRUPT")
        raise PatchError(
            f"operation {operation['id']} was interrupted and is not idempotent"
        )
    _assert_write_policy(environment)
    if operation["rollback"] == "IRREVERSIBLE" and not approval.get(
        "allow_irreversible"
    ):
        raise PatchError(
            f"operation {operation['id']} now requires an irreversible write; "
            "approve the plan with --allow-irreversible"
        )
    if operation["rollback"] == "RESTORE" and receipt["backup_completed"]:
        _verify_backup(operation, receipt, path, context, kit_dir)
    if operation["rollback"] == "RESTORE" and not receipt["backup_completed"]:
        assert_target_identity(
            manifest,
            environment,
            context,
            kit_dir,
            operation["phases"]["backup"],
        )
        save_receipt(path, receipt, "BACKUP_INTENT")
        result = _run_phase(operation, "backup", receipt, path, context, kit_dir)
        if result["returncode"] != 0:
            save_receipt(path, receipt, "BACKUP_FAILED")
            raise PatchError(f"operation {operation['id']} backup failed")
        receipt["backup_identity"] = result["metadata"]
        receipt["backup_completed"] = True
        save_receipt(path, receipt, "BACKED_UP")
        _verify_backup(operation, receipt, path, context, kit_dir)

    assert_target_identity(
        manifest,
        environment,
        context,
        kit_dir,
        operation["phases"]["apply"],
    )
    receipt["changed"] = True
    save_receipt(path, receipt, "APPLY_INTENT")
    result = _run_phase(operation, "apply", receipt, path, context, kit_dir)
    if result["returncode"] != 0:
        save_receipt(path, receipt, "APPLY_FAILED")
        raise PatchError(f"operation {operation['id']} apply failed")
    save_receipt(path, receipt, "APPLIED")

    state = _check_operation(operation, receipt, path, context, kit_dir)
    receipt["observed_state"] = state
    if state != "TARGET":
        save_receipt(path, receipt, f"POST_APPLY_{state}")
        raise PatchError(
            f"operation {operation['id']} did not reach TARGET after apply: {state}"
        )
    _verify_operation(operation, receipt, path, context, kit_dir, "VERIFIED")


def execute_plan(run_dir: Path) -> JsonObject:
    run_dir = run_dir.resolve()
    with RunLock(run_dir):
        plan, manifest, environment, _, model, kit_dir = _load_bound_inputs(run_dir)
        approval = _assert_approved(run_dir, plan)
        if (run_dir / "ROLLBACK.json").exists():
            raise PatchError("run is rolled back; create and approve a new plan")
        _invalidate_delivery(run_dir)
        context = command_context(manifest, environment, kit_dir, run_dir)
        order = selected_operation_order(model)
        pending_manual: list[str] = []
        blocked_by_dependency: list[str] = []

        for operation_id in order:
            operation = model["operations"][operation_id]
            path = operation_receipt_path(run_dir, operation_id)
            receipt = load_receipt(path, operation_id, plan)
            if receipt["status"] in ROLLBACK_STATUSES:
                raise PatchError(
                    f"operation {operation_id} entered rollback; continue rollback "
                    "or create a new plan"
                )
            if receipt["status"] in COMPLETE_STATUSES:
                assert_target_identity(
                    manifest,
                    environment,
                    context,
                    kit_dir,
                    operation["phases"]["check"],
                )
                state = _check_operation(
                    operation,
                    receipt,
                    path,
                    context,
                    kit_dir,
                )
                receipt["observed_state"] = state
                if state in {"DRIFT", "UNKNOWN"}:
                    save_receipt(path, receipt, f"HALTED_{state}")
                    raise PatchError(
                        f"completed operation {operation_id} live state is {state}"
                    )
                if state != "TARGET":
                    save_receipt(path, receipt, f"REGRESSED_{state}")
                    raise PatchError(
                        f"completed operation {operation_id} regressed to {state}"
                    )
                _verify_operation(
                    operation,
                    receipt,
                    path,
                    context,
                    kit_dir,
                    receipt["status"],
                )
                continue
            dependencies = [
                load_receipt(
                    operation_receipt_path(run_dir, dependency),
                    dependency,
                    plan,
                )["status"]
                for dependency in operation["depends_on"]
            ]
            if not all(status in COMPLETE_STATUSES for status in dependencies):
                blocked_by_dependency.append(operation_id)
                continue
            if operation["execution"] == "MANUAL":
                assert_target_identity(
                    manifest,
                    environment,
                    context,
                    kit_dir,
                    operation["phases"]["check"],
                )
                state = _check_operation(
                    operation,
                    receipt,
                    path,
                    context,
                    kit_dir,
                )
                receipt["observed_state"] = state
                if state in {"DRIFT", "UNKNOWN"}:
                    save_receipt(path, receipt, f"HALTED_{state}")
                    raise PatchError(
                        f"manual operation {operation_id} live state is {state}"
                    )
                if state == "TARGET":
                    if (
                        operation["rollback"] == "RESTORE"
                        and receipt["changed"]
                    ):
                        if not receipt["backup_completed"]:
                            raise PatchError(
                                f"manual operation {operation_id} reached TARGET "
                                "without a completed backup"
                            )
                        _verify_backup(
                            operation,
                            receipt,
                            path,
                            context,
                            kit_dir,
                        )
                    if receipt["status"] != "MANUAL_REQUIRED":
                        save_receipt(path, receipt, "MANUAL_REQUIRED")
                    pending_manual.append(operation_id)
                    print(
                        f"MANUAL_ACK_REQUIRED {operation_id}: live state is TARGET; "
                        "do not repeat the manual operation; attach redacted evidence "
                        "with ack-manual"
                    )
                    continue
                _assert_write_policy(environment)
                if operation["rollback"] == "IRREVERSIBLE" and not approval.get(
                    "allow_irreversible"
                ):
                    raise PatchError(
                        f"manual operation {operation_id} requires an irreversible "
                        "write; approve the plan with --allow-irreversible"
                    )
                if (
                    operation["rollback"] == "RESTORE"
                    and not receipt["backup_completed"]
                ):
                    assert_target_identity(
                        manifest,
                        environment,
                        context,
                        kit_dir,
                        operation["phases"]["backup"],
                    )
                    save_receipt(path, receipt, "BACKUP_INTENT")
                    result = _run_phase(
                        operation,
                        "backup",
                        receipt,
                        path,
                        context,
                        kit_dir,
                    )
                    if result["returncode"] != 0:
                        save_receipt(path, receipt, "BACKUP_FAILED")
                        raise PatchError(
                            f"manual operation {operation_id} backup failed"
                        )
                    receipt["backup_identity"] = result["metadata"]
                    receipt["backup_completed"] = True
                    save_receipt(path, receipt, "BACKED_UP")
                    _verify_backup(
                        operation,
                        receipt,
                        path,
                        context,
                        kit_dir,
                    )
                elif operation["rollback"] == "RESTORE" and receipt["backup_completed"]:
                    _verify_backup(
                        operation,
                        receipt,
                        path,
                        context,
                        kit_dir,
                    )
                receipt["changed"] = True
                save_receipt(path, receipt, "MANUAL_REQUIRED")
                pending_manual.append(operation_id)
                print(
                    f"MANUAL_REQUIRED {operation_id}: {operation['manual']['instructions']}"
                )
                continue
            _execute_compiled(
                operation,
                receipt,
                path,
                context,
                kit_dir,
                approval,
                manifest,
                environment,
            )

        return {
            "plan_fingerprint": plan["plan_fingerprint"],
            "pending_manual": pending_manual,
            "blocked_by_dependency": blocked_by_dependency,
            "complete": not pending_manual and not blocked_by_dependency,
        }


def acknowledge_manual(
    run_dir: Path, operation_id: str, evidence_path: Path
) -> JsonObject:
    run_dir = run_dir.resolve()
    with RunLock(run_dir):
        plan, manifest, environment, _, model, kit_dir = _load_bound_inputs(run_dir)
        _assert_approved(run_dir, plan)
        _invalidate_delivery(run_dir)
        if operation_id not in model["operations"]:
            raise PatchError(f"unknown operation: {operation_id}")
        operation = model["operations"][operation_id]
        if operation_id not in selected_operation_order(model):
            raise PatchError(f"operation is not selected: {operation_id}")
        if operation["execution"] != "MANUAL":
            raise PatchError(f"operation is not MANUAL: {operation_id}")
        incomplete_dependencies = [
            dependency
            for dependency in operation["depends_on"]
            if load_receipt(
                operation_receipt_path(run_dir, dependency),
                dependency,
                plan,
            )["status"]
            not in COMPLETE_STATUSES
        ]
        if incomplete_dependencies:
            raise PatchError(
                "manual operation has incomplete dependencies: "
                + ", ".join(incomplete_dependencies)
            )
        context = command_context(manifest, environment, kit_dir, run_dir)
        assert_target_identity(
            manifest,
            environment,
            context,
            kit_dir,
            operation["phases"]["check"],
        )
        for dependency in operation["depends_on"]:
            dependency_operation = model["operations"][dependency]
            dependency_path = operation_receipt_path(run_dir, dependency)
            dependency_receipt = load_receipt(
                dependency_path,
                dependency,
                plan,
            )
            assert_target_identity(
                manifest,
                environment,
                context,
                kit_dir,
                dependency_operation["phases"]["check"],
            )
            state = _check_operation(
                dependency_operation,
                dependency_receipt,
                dependency_path,
                context,
                kit_dir,
            )
            if state != "TARGET":
                if state in {"BASE", "ABSENT"}:
                    save_receipt(
                        dependency_path,
                        dependency_receipt,
                        f"REGRESSED_{state}",
                    )
                else:
                    save_receipt(
                        dependency_path,
                        dependency_receipt,
                        f"HALTED_{state}",
                    )
                raise PatchError(
                    f"manual dependency {dependency} live state is {state}"
                )
            _verify_operation(
                dependency_operation,
                dependency_receipt,
                dependency_path,
                context,
                kit_dir,
                dependency_receipt["status"],
            )
        evidence_path = evidence_path.resolve(strict=True)
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise PatchError("manual evidence must be a regular non-symlink file")

        path = operation_receipt_path(run_dir, operation_id)
        receipt = load_receipt(path, operation_id, plan)
        if receipt["status"] not in {"MANUAL_REQUIRED", "VERIFY_FAILED"}:
            raise PatchError(
                f"manual operation is not awaiting evidence: {operation_id}"
            )
        receipt["manual_evidence"] = {
            "path": str(evidence_path),
            "bytes": evidence_path.stat().st_size,
            "sha256": file_sha256(evidence_path),
        }
        if operation["rollback"] == "RESTORE" and receipt["changed"]:
            if not receipt["backup_completed"]:
                raise PatchError(
                    f"manual operation {operation_id} has no completed backup"
                )
            _verify_backup(
                operation,
                receipt,
                path,
                context,
                kit_dir,
            )
        save_receipt(path, receipt, "MANUAL_ACKED")
        state = _check_operation(
            operation,
            receipt,
            path,
            context,
            kit_dir,
        )
        receipt["observed_state"] = state
        if state != "TARGET":
            status = (
                f"REGRESSED_{state}"
                if state in {"BASE", "ABSENT"}
                else f"HALTED_{state}"
            )
            save_receipt(path, receipt, status)
            raise PatchError(f"manual operation {operation_id} live state is {state}")
        _verify_operation(
            operation,
            receipt,
            path,
            context,
            kit_dir,
            "MANUAL_VERIFIED",
        )
        return receipt


def verify_plan(run_dir: Path) -> JsonObject:
    run_dir = run_dir.resolve()
    with RunLock(run_dir):
        plan, manifest, environment, _, model, kit_dir = _load_bound_inputs(run_dir)
        _assert_approved(run_dir, plan)
        _invalidate_delivery(run_dir)
        context = command_context(manifest, environment, kit_dir, run_dir)
        assert_target_identity(
            manifest,
            environment,
            context,
            kit_dir,
        )
        order = selected_operation_order(model)
        pending = []
        for operation_id in order:
            operation = model["operations"][operation_id]
            path = operation_receipt_path(run_dir, operation_id)
            receipt = load_receipt(path, operation_id, plan)
            if receipt["status"] not in COMPLETE_STATUSES:
                pending.append(operation_id)
                continue
            assert_target_identity(
                manifest,
                environment,
                context,
                kit_dir,
                operation["phases"]["check"],
            )
            state = _check_operation(
                operation,
                receipt,
                path,
                context,
                kit_dir,
            )
            receipt["observed_state"] = state
            if state != "TARGET":
                status = (
                    f"REGRESSED_{state}"
                    if state in {"BASE", "ABSENT"}
                    else f"HALTED_{state}"
                )
                save_receipt(path, receipt, status)
                raise PatchError(f"operation {operation_id} live state is {state}")
            final_status = (
                "MANUAL_VERIFIED"
                if operation["execution"] == "MANUAL"
                else receipt["status"]
            )
            _verify_operation(
                operation,
                receipt,
                path,
                context,
                kit_dir,
                final_status,
            )
        if pending:
            raise PatchError(f"operations are incomplete: {', '.join(pending)}")

        acceptance_status: dict[str, str] = {}
        for check in model["acceptance"].values():
            if not model["enabled_features"].intersection(check["features"]):
                continue
            assert_target_identity(
                manifest,
                environment,
                context,
                kit_dir,
                check["command"],
            )
            result = run_command(
                check["command"],
                context,
                kit_dir,
                acceptance=check,
            )
            status = "PASSED" if result["returncode"] == 0 else "FAILED"
            acceptance_receipt = {
                "schema_version": 1,
                "check_id": check["id"],
                "plan_fingerprint": plan["plan_fingerprint"],
                "status": status,
                "result": result,
                "updated_at": utc_now(),
            }
            seal(acceptance_receipt, "receipt_fingerprint")
            write_json_atomic(
                acceptance_receipt_path(run_dir, check["id"]),
                acceptance_receipt,
            )
            acceptance_status[check["id"]] = status
            if status == "FAILED":
                raise PatchError(f"acceptance check failed: {check['id']}")

        feature_status: dict[str, str] = {}
        for feature_id in sorted(model["enabled_features"]):
            operation_ids = [
                operation["id"]
                for operation in model["operations"].values()
                if feature_id in operation["features"]
            ]
            check_ids = [
                check["id"]
                for check in model["acceptance"].values()
                if feature_id in check["features"]
            ]
            operations_pass = all(
                load_receipt(
                    operation_receipt_path(run_dir, operation_id),
                    operation_id,
                    plan,
                )["status"]
                in COMPLETE_STATUSES
                for operation_id in operation_ids
            )
            checks_pass = all(
                acceptance_status.get(check_id) == "PASSED" for check_id in check_ids
            )
            feature_status[feature_id] = (
                "COMPLETE" if operations_pass and checks_pass else "INCOMPLETE"
            )
        report = {
            "schema_version": 1,
            "plan_fingerprint": plan["plan_fingerprint"],
            "features": feature_status,
            "acceptance": acceptance_status,
            "verified_at": utc_now(),
        }
        write_json_atomic(run_dir / "VERIFY.json", report)
        if any(status != "COMPLETE" for status in feature_status.values()):
            raise PatchError("one or more enabled features are incomplete")
        delivery: JsonObject = {
            "schema_version": 1,
            "status": "VERIFIED",
            "patch": plan["patch"],
            "target": {
                "account": plan["target"]["account"],
                "region": plan["target"]["region"],
                **(
                    {
                        "baseline_snapshot_sha256": plan["target"]["baseline"][
                            "sha256"
                        ]
                    }
                    if "baseline" in plan["target"]
                    else {}
                ),
            },
            "plan_fingerprint": plan["plan_fingerprint"],
            "features": feature_status,
            "acceptance": acceptance_status,
            "verified_at": report["verified_at"],
        }
        provenance = manifest.get("provenance")
        if isinstance(provenance, dict):
            delivery["public_gateway"] = {
                "repository": provenance["public_gateway"]["repository"],
            }
        seal(delivery, "delivery_fingerprint")
        write_json_atomic(run_dir / "DELIVERY.json", delivery)
        report["delivery_fingerprint"] = delivery["delivery_fingerprint"]
        write_json_atomic(run_dir / "VERIFY.json", report)
        return report


def rollback_plan(run_dir: Path) -> JsonObject:
    run_dir = run_dir.resolve()
    with RunLock(run_dir):
        plan, manifest, environment, _, model, kit_dir = _load_bound_inputs(run_dir)
        approval = _assert_approved(run_dir, plan)
        _invalidate_delivery(run_dir)
        context = command_context(manifest, environment, kit_dir, run_dir)
        results: dict[str, str] = {}

        for operation_id in reversed(selected_operation_order(model)):
            operation = model["operations"][operation_id]
            path = operation_receipt_path(run_dir, operation_id)
            receipt = load_receipt(path, operation_id, plan)
            if not receipt["changed"]:
                results[operation_id] = "UNCHANGED"
                continue
            if receipt["status"] in {
                "RETAINED",
                "IRREVERSIBLE_RETAINED",
            }:
                results[operation_id] = receipt["status"]
                continue
            if operation["rollback"] == "RETAIN":
                save_receipt(path, receipt, "RETAINED")
                results[operation_id] = "RETAINED"
                continue
            if operation["rollback"] == "IRREVERSIBLE":
                if not approval.get("allow_irreversible"):
                    raise PatchError(
                        f"unapproved IRREVERSIBLE operation: {operation_id}"
                    )
                save_receipt(path, receipt, "IRREVERSIBLE_RETAINED")
                results[operation_id] = "IRREVERSIBLE_RETAINED"
                continue
            assert_target_identity(
                manifest,
                environment,
                context,
                kit_dir,
                operation["phases"]["rollback"],
            )
            if not receipt["backup_completed"]:
                raise PatchError(f"operation {operation_id} has no completed backup")
            _verify_backup(
                operation,
                receipt,
                path,
                context,
                kit_dir,
                failure_status="ROLLBACK_BACKUP_INVALID",
                clear_on_failure=False,
            )
            assert_target_identity(
                manifest,
                environment,
                context,
                kit_dir,
                operation["phases"]["check"],
            )
            state = _check_operation(
                operation,
                receipt,
                path,
                context,
                kit_dir,
            )
            receipt["observed_state"] = state
            if state in {"DRIFT", "UNKNOWN"}:
                save_receipt(path, receipt, f"ROLLBACK_HALTED_{state}")
                raise PatchError(
                    f"operation {operation_id} cannot roll back from {state}"
                )
            if state != "TARGET":
                result = _run_phase(
                    operation,
                    "rollback_verify",
                    receipt,
                    path,
                    context,
                    kit_dir,
                )
                if result["returncode"] == 0:
                    save_receipt(path, receipt, "ROLLED_BACK")
                    results[operation_id] = "ROLLED_BACK"
                    continue
                save_receipt(path, receipt, "HALTED_ROLLBACK_STATE")
                raise PatchError(
                    f"operation {operation_id} is neither TARGET nor verified "
                    "as rolled back"
                )
            save_receipt(path, receipt, "ROLLBACK_INTENT")
            result = _run_phase(operation, "rollback", receipt, path, context, kit_dir)
            if result["returncode"] != 0:
                save_receipt(path, receipt, "ROLLBACK_FAILED")
                raise PatchError(f"operation {operation_id} rollback failed")
            result = _run_phase(
                operation,
                "rollback_verify",
                receipt,
                path,
                context,
                kit_dir,
            )
            if result["returncode"] != 0:
                save_receipt(path, receipt, "ROLLBACK_VERIFY_FAILED")
                raise PatchError(
                    f"operation {operation_id} rollback verification failed"
                )
            save_receipt(path, receipt, "ROLLED_BACK")
            results[operation_id] = "ROLLED_BACK"

        report = {
            "schema_version": 1,
            "plan_fingerprint": plan["plan_fingerprint"],
            "operations": results,
            "rolled_back_at": utc_now(),
        }
        write_json_atomic(run_dir / "ROLLBACK.json", report)
        return report
