#!/usr/bin/env python3
"""Locking and integrity-checked receipt storage for claw-patch-v2."""

from __future__ import annotations

import fcntl
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from _patch_context import validate_backup_metadata
from _patch_model import (
    JsonObject,
    PatchError,
    fingerprint,
    load_json,
    write_json_atomic,
)

COMPLETE_STATUSES = {"VERIFIED", "SKIPPED", "MANUAL_VERIFIED"}
RECEIPT_STATUSES = {
    "PENDING",
    "BACKUP_INTENT",
    "BACKUP_FAILED",
    "BACKUP_INVALID",
    "BACKED_UP",
    "APPLY_INTENT",
    "APPLY_FAILED",
    "APPLIED",
    "RECONCILE_INTENT",
    "RECONCILE_FAILED",
    "VERIFY_FAILED",
    "VERIFIED",
    "SKIPPED",
    "MANUAL_REQUIRED",
    "MANUAL_ACKED",
    "MANUAL_VERIFIED",
    "UNKNOWN_AFTER_INTERRUPT",
    "HALTED_DRIFT",
    "HALTED_UNKNOWN",
    "REGRESSED_BASE",
    "REGRESSED_ABSENT",
    "HALTED_ROLLBACK_STATE",
    "POST_APPLY_BASE",
    "POST_APPLY_ABSENT",
    "POST_APPLY_DRIFT",
    "POST_APPLY_UNKNOWN",
    "POST_RECONCILE_BASE",
    "POST_RECONCILE_ABSENT",
    "POST_RECONCILE_DRIFT",
    "POST_RECONCILE_UNKNOWN",
    "ROLLBACK_INTENT",
    "ROLLBACK_FAILED",
    "ROLLBACK_BACKUP_INVALID",
    "ROLLBACK_HALTED_DRIFT",
    "ROLLBACK_HALTED_UNKNOWN",
    "ROLLBACK_VERIFY_FAILED",
    "ROLLED_BACK",
    "RETAINED",
    "IRREVERSIBLE_RETAINED",
}
ROLLBACK_STATUSES = {
    "ROLLBACK_INTENT",
    "ROLLBACK_FAILED",
    "ROLLBACK_BACKUP_INVALID",
    "ROLLBACK_HALTED_DRIFT",
    "ROLLBACK_HALTED_UNKNOWN",
    "ROLLBACK_VERIFY_FAILED",
    "ROLLED_BACK",
    "RETAINED",
    "IRREVERSIBLE_RETAINED",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunLock(AbstractContextManager["RunLock"]):
    """Hold an exclusive advisory lock for one run directory."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / ".lock"
        self.handle: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise PatchError(f"another patchctl process holds {self.path}") from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def seal(value: JsonObject, field: str) -> JsonObject:
    value.pop(field, None)
    value[field] = fingerprint(value)
    return value


def validate_seal(value: JsonObject, field: str, label: str) -> None:
    expected = value.get(field)
    core = {key: item for key, item in value.items() if key != field}
    if not isinstance(expected, str) or fingerprint(core) != expected:
        raise PatchError(f"{label} fingerprint is invalid")


def operation_receipt_path(run_dir: Path, operation_id: str) -> Path:
    return run_dir / "receipts" / "operations" / f"{operation_id}.json"


def acceptance_receipt_path(run_dir: Path, check_id: str) -> Path:
    return run_dir / "receipts" / "acceptance" / f"{check_id}.json"


def load_receipt(path: Path, operation_id: str, plan: JsonObject) -> JsonObject:
    if not path.exists():
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "plan_fingerprint": plan["plan_fingerprint"],
            "status": "PENDING",
            "changed": False,
            "backup_completed": False,
            "phase_results": {},
        }
    receipt = load_json(path)
    validate_seal(receipt, "receipt_fingerprint", f"receipt for {operation_id}")
    if receipt.get("operation_id") != operation_id:
        raise PatchError(f"receipt operation id mismatch for {operation_id}")
    if receipt.get("plan_fingerprint") != plan["plan_fingerprint"]:
        raise PatchError(f"receipt for {operation_id} belongs to a different plan")
    if receipt.get("status") not in RECEIPT_STATUSES:
        raise PatchError(f"receipt for {operation_id} has an invalid status")
    if not isinstance(receipt.get("changed"), bool) or not isinstance(
        receipt.get("backup_completed"), bool
    ):
        raise PatchError(f"receipt for {operation_id} has invalid state flags")
    if not isinstance(receipt.get("phase_results"), dict):
        raise PatchError(f"receipt for {operation_id} has invalid phase results")
    if receipt["backup_completed"]:
        validate_backup_metadata(receipt.get("backup_identity"))
    return receipt


def save_receipt(path: Path, receipt: JsonObject, status: str) -> None:
    if status not in RECEIPT_STATUSES:
        raise PatchError(f"refusing to write unknown receipt status: {status}")
    receipt["status"] = status
    receipt["updated_at"] = utc_now()
    seal(receipt, "receipt_fingerprint")
    write_json_atomic(path, receipt)
