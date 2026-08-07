#!/usr/bin/env python3
"""Evidence-backed state probe for operations without a compiled adapter."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

TARGET = 0
BASE = 10
UNKNOWN = 13


def _marker(operation_id: str, run_dir: str) -> Path:
    return Path(run_dir) / "manual" / f"{operation_id}.json"


def _state(operation_id: str, run_dir: str) -> tuple[int, bytes | None]:
    path = _marker(operation_id, run_dir)
    if not path.exists():
        return BASE, None
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        return UNKNOWN, None
    if value == {"operation_id": operation_id, "status": "TARGET"}:
        return TARGET, payload
    return UNKNOWN, None


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        raise SystemExit("usage: manual_gate.py ACTION OPERATION_ID RUN_DIR PROOF")
    action, operation_id, run_dir, proof = argv[1:]
    state, payload = _state(operation_id, run_dir)
    if action == "state":
        return state
    if action == "verify":
        return 0 if state == TARGET else 1
    if action != "accept" or state != TARGET or payload is None:
        return 1
    challenge = os.environ["CLAW_PATCH_ACCEPTANCE_CHALLENGE"]
    observation = hashlib.sha256(payload).hexdigest()
    proof_observation = hashlib.sha256(
        json.dumps(
            {"observation": observation, "proof": proof},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    evidence = hashlib.sha256(
        json.dumps(
            {
                "challenge": challenge,
                "check_id": f"{operation_id}-acceptance",
                "proof_id": proof,
                "observation_sha256": proof_observation,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    print(
        json.dumps(
            {
                "schema_version": 1,
                "check_id": f"{operation_id}-acceptance",
                "challenge_sha256": hashlib.sha256(challenge.encode()).hexdigest(),
                "proofs": [
                    {
                        "id": proof,
                        "observation_sha256": proof_observation,
                        "evidence_sha256": evidence,
                    }
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
