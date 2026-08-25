# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Recognise the event shapes this function accepts, and refuse the rest by RETURNING (#515 #21).

Measured during the 2026-08-17 restorepatch application: the kit's liveness probe sends
`{"path": "/ping"}`. That matched none of the recognised shapes, so the dispatch fell through to
`event["httpMethod"]` and raised `KeyError` for every such caller — the function never reached its
router. The kit then printed `a 404 body on a private API is expected` and reported 11 pass / 0 fail,
so "can the function execute at all" was never verified.

Hence: refuse by returning a self-describing 400. A returned response is itself proof the function
executed, which is exactly what a liveness probe needs; an exception sets FunctionError and is the
signal that got misread as success.

Lives in its own module (no boto3, no table names) so the behaviour is unit-testable without
standing up clients — the previous version of this check could only be asserted as text.
"""

from __future__ import annotations

import json

SUPPORTED_SHAPES = (
    "API Gateway proxy (httpMethod+resource) / SQS Records / dispatch.poller "
    "/ credential.reconciler"
)
UNSUPPORTED_CODE = "UNSUPPORTED_EVENT_SHAPE"
MAX_ECHOED_KEYS = 12


def unsupported_event_response(event: dict) -> dict | None:
    """A 400 response for an unrecognised payload, or None when the dispatch should continue.

    Only the API Gateway proxy shape is decided here; SQS and the poller are handled earlier in the
    dispatch, so reaching this with `httpMethod` present means the normal path.
    """
    if not isinstance(event, dict):
        event = {}
    if "httpMethod" in event:
        return None
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "error": "unsupported event shape",
                "code": UNSUPPORTED_CODE,
                "detail": (
                    "this function was invoked directly with a payload that is none of: "
                    f"{SUPPORTED_SHAPES}. The function itself is healthy — it executed and "
                    "refused the payload. A liveness probe should treat this 400 as proof of "
                    "life, and must not treat a FunctionError as an expected 404."
                ),
                # Key NAMES only, and capped: a direct invoke is attacker-reachable in the worst
                # case, so the echo must help a operator without reflecting payload values.
                "event_keys": sorted(str(k) for k in event)[:MAX_ECHOED_KEYS],
            }
        ),
    }
