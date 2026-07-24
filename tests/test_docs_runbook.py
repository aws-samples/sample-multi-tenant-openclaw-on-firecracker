# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guard tests for docs/RUNBOOK.md (T3-7).

Docs rot faster than code. This turns the most load-bearing runbook claims —
the operator endpoints it tells you to curl, the CloudWatch alarm names it tells
you to watch — into CI failures when the code they reference is renamed or
removed. It is deliberately shallow: existence checks, not prose review.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
HANDLER = (ROOT / "deploy" / "lambda" / "api" / "handler.py").read_text()
STACK = (ROOT / "deploy" / "stack.py").read_text()

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def runbook():
    assert RUNBOOK.is_file(), "docs/RUNBOOK.md is missing"
    return RUNBOOK.read_text()


def test_runbook_is_substantial(runbook):
    # A stub file would silently pass the other checks; require real content.
    assert len(runbook) > 5000, "RUNBOOK.md is suspiciously short"
    for heading in ("Deploy", "Operator endpoints", "Observability",
                    "failure modes", "Escalation"):
        assert heading in runbook, f"RUNBOOK missing a '{heading}' section"


def test_referenced_operator_endpoints_exist(runbook):
    """Every operator endpoint the runbook tells you to call must resolve to a
    real route (or the {id}/{action} router) in the API handler."""
    # T2-8 + core operator surface the runbook documents.
    assert "/failover/{az}" in HANDLER
    assert "/hosts/{instance_id}/drain" in HANDLER
    # cancel-migration + backup go through the POST /tenants/{id}/{action} router.
    assert '"cancel-migration"' in HANDLER or "cancel-migration" in HANDLER
    assert '("POST", "/tenants/{id}/{action}")' in HANDLER
    # If the runbook mentions them, the code must still expose them.
    if "cancel-migration" in runbook:
        assert "cancel-migration" in HANDLER
    if "/drain" in runbook:
        assert "/hosts/{instance_id}/drain" in HANDLER
    if "failover/" in runbook:
        assert '"/failover/{az}"' in HANDLER


def test_referenced_alarm_names_exist_in_stack(runbook):
    """Alarm names the runbook lists in its observability table must match the
    names stack.py actually creates.

    The per-Lambda error/throttle alarms are built from an f-string
    (`alarm_name=f"openclaw-{_label.lower()}-errors"`) over a label list, so we
    reconstruct those from the loop's label tuple rather than expecting literal
    names; the DLQ alarms are literal `alarm_name="..."`.
    """
    literal = set(re.findall(r"alarm_name=[\"']([a-z0-9-]+)[\"']", STACK))
    # Labels feeding the per-Lambda alarm loop: (fn, "Api"), (fn, "Health"), ...
    labels = re.findall(r'\(\w+_fn,\s*"([A-Za-z]+)"\)', STACK)
    templated = set()
    for lbl in labels:
        templated.add(f"openclaw-{lbl.lower()}-errors")
        templated.add(f"openclaw-{lbl.lower()}-throttles")
    all_alarms = literal | templated

    # Every backtick-quoted openclaw-*-{errors,throttles,not-empty} name the
    # runbook prints must be a real alarm.
    cited = set(re.findall(r"`(openclaw-[a-z0-9-]+(?:-errors|-throttles|-not-empty))`", runbook))
    assert cited, "RUNBOOK observability table cites no alarm names"
    missing = cited - all_alarms
    assert not missing, f"RUNBOOK cites alarms stack.py never creates: {sorted(missing)}"


def test_new_failover_statuses_documented(runbook):
    """T3-2 introduced the async failover statuses; the runbook must explain
    them so an operator seeing failover_queued knows what it means."""
    for status in ("failover_queued", "failover_recovering", "failover_failed"):
        assert status in runbook, f"RUNBOOK does not document the {status!r} status"
