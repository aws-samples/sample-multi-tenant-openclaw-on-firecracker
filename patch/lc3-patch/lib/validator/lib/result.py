import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


VERDICTS = {"PASS", "FAIL", "INCONCLUSIVE", "UNVERIFIED"}


@dataclass
class Finding:
    id: str
    group: str
    verdict: str
    summary: str
    readings: Dict[str, Any] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    remediation: str = ""

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise ValueError("unsupported verdict: %s" % self.verdict)
        if not isinstance(self.readings, dict) or not self.readings:
            raise ValueError("every finding must carry non-empty readings")
        if not self.remediation:
            raise ValueError("every finding must carry remediation")

    def to_dict(self):
        return asdict(self)


def finding(check_id, verdict, summary, readings, evidence=None, remediation=None):
    return Finding(
        id=check_id,
        group=check_id[0],
        verdict=verdict,
        summary=summary,
        readings=readings,
        evidence=list(evidence or []),
        remediation=remediation or "Review the readings and resolve the discrepancy.",
    )


def flatten(items):
    rows = []
    for item in items:
        rows.extend(item if isinstance(item, list) else [item])
    return rows


def report_document(rows, metadata=None):
    return {
        "metadata": dict(metadata or {}),
        "summary": verdict_counts(rows),
        "findings": [row.to_dict() for row in rows],
    }


def verdict_counts(rows):
    counts = {name: 0 for name in sorted(VERDICTS)}
    for row in rows:
        counts[row.verdict] += 1
    return counts


def dumps(rows, metadata=None):
    return json.dumps(report_document(rows, metadata), indent=2, sort_keys=True)


def exit_code(rows):
    verdicts = {row.verdict for row in rows}
    if "FAIL" in verdicts:
        return 1
    if verdicts.intersection({"INCONCLUSIVE", "UNVERIFIED"}):
        return 2
    return 0


def ensure_nonempty(rows, check_id, inspected_name):
    values = list(rows)
    if values:
        return values
    return [finding(
        check_id,
        "INCONCLUSIVE",
        "Nothing was available to inspect.",
        {"inspected": 0, "collection": inspected_name},
        remediation="Supply the missing environment coordinates and run again.",
    )]
