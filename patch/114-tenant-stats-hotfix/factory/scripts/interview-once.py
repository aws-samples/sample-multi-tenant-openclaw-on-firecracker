#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ask everything about this patch ONCE, then hand the run a signed decision file.

Why this exists: an unattended rollout must not stop mid-flight to ask a question it could
have asked up front. So every human decision this patch needs is derived from the kit,
printed as one batch, answered once, and recorded. After that the driver runs to completion
or stops on a machine gate — never on a missing answer.

The questions are DERIVED, never a fixed list: params_changed entries, manual operations,
rollback policies and verification phases all come out of the manifest. A kit that declares
nothing to decide produces no questions; a kit that hides a concurrency bump inside a code
diff cannot, because `params_changed` is mandatory.

Modes:
  ask   <kit>                 print the batch of questions (read-only)
  record <kit> <answers.json> validate answers, write DECISION.json
  check <kit>                 exit 0 only if a valid DECISION.json covers every question
"""

import hashlib
import json
import os
import sys


def load(kit):
    with open(os.path.join(kit, "manifest.json")) as handle:
        return json.load(handle)


def manifest_fingerprint(kit):
    """Answers are bound to the exact manifest they were given for. Re-answering is cheap;
    silently reusing yesterday's approval for a different patch is not."""
    with open(os.path.join(kit, "manifest.json"), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def qid(*parts):
    """Question ids are machine keys, so they must be safe to grep, quote and use as a shell
    word: no spaces, no shell metacharacters. Free-text (a parameter name like
    'Buffer_Chunk_Size / Buffer_Max_Size') is slugged, and a short hash keeps two different
    names from colliding after slugging."""
    raw = "::".join(str(p) for p in parts)
    slug = "".join(c if (c.isalnum() or c in "-_.:/") else "-" for c in raw)
    while "--" in slug:
        slug = slug.replace("--", "-")
    if slug != raw:
        slug = f"{slug.strip('-')}~{hashlib.sha256(raw.encode()).hexdigest()[:6]}"
    return slug


def questions(m):
    """Derive every decision this kit actually requires."""
    out = []

    out.append(
        {
            "id": "Q-scope",
            "topic": "scope",
            "ask": (
                f"This kit is {m['id']} at patch_sha {m['patch_sha'][:12]}, status "
                f"{m['status']}, covering {len(m['paths'])} path(s). Confirm this is the "
                f"patch you intend to roll out."
            ),
            "expects": "yes/no",
            "blocking": True,
        }
    )

    for path, spec in m["paths"].items():
        act = spec.get("activation") or {}
        if act:
            out.append(
                {
                    "id": qid("Q-activate", path),
                    "topic": "activation",
                    "ask": (
                        f"{path} installs to {act.get('dest_path')} and activates by "
                        f"{act.get('action')} on {act.get('target')}. That restart is a brief "
                        f"interruption of that service on each host. Approved?"
                    ),
                    "expects": "yes/no",
                    "blocking": True,
                }
            )

    for fix in m.get("fixes", []):
        for p in fix.get("params_changed", []):
            out.append(
                {
                    "id": qid("Q-param", fix["id"], p["name"]),
                    "topic": "runtime-parameter",
                    "ask": (
                        f"{fix['id']} changes {p['name']} ({p['layer']}) from "
                        f"{p['old_default']} to {p['new_default']}. Stated impact: "
                        f"{p['runtime_impact']} Accept this new value?"
                    ),
                    "expects": "yes/no",
                    "blocking": True,
                }
            )

    for path, spec in m["paths"].items():
        for op in spec.get("operations", []):
            if op.get("class") == "MANUAL_CLI_REVIEW":
                out.append(
                    {
                        "id": qid("Q-manual", path),
                        "topic": "manual-operation",
                        "ask": (
                            f"{op['resource']} has no automated lane"
                            + (f" ({op['why_manual']})" if op.get("why_manual") else "")
                            + ". It will NOT be applied by this run. Acknowledge that it "
                            "stays for a human, and say who owns it."
                        ),
                        "expects": "owner name, or 'defer'",
                        "blocking": True,
                    }
                )
            if op.get("class") == "UNPATCHABLE":
                out.append(
                    {
                        "id": qid("Q-blocked", path),
                        "topic": "unpatchable",
                        "ask": (
                            f"{op['resource']} has no safe manual path either. This kit "
                            "cannot deliver it at all. Proceed without it?"
                        ),
                        "expects": "yes/no",
                        "blocking": True,
                    }
                )

    policies = sorted(
        {
            op.get("rollback_policy", "UNSET")
            for spec in m["paths"].values()
            for op in spec.get("operations", [])
        }
    )
    out.append(
        {
            "id": "Q-rollback",
            "topic": "rollback",
            "ask": (
                f"Rollback policies in this kit: {', '.join(policies)}. If the canary fails "
                f"verification, the run stops and `rollback.sh` restores the backup. Confirm "
                f"you accept these policies — and note that RETAIN means a rollback will NOT "
                f"undo that operation."
            ),
            "expects": "yes/no",
            "blocking": True,
        }
    )

    phases = sorted({v.get("phase", "?") for v in m.get("verifications", [])})
    if any(p.startswith("B-") for p in phases):
        out.append(
            {
                "id": "Q-lifecycle-verification",
                "topic": "verification",
                "ask": (
                    "This kit declares phase-B (lifecycle) verifications, which create real "
                    "tenants and then delete them. Approve running them on this environment, "
                    "and confirm the teardown will delete ONLY the exact ids it created?"
                ),
                "expects": "yes/no",
                "blocking": True,
            }
        )

    out.append(
        {
            "id": "Q-fleet-widening",
            "topic": "blast-radius",
            "ask": (
                "After the canary passes verification the run needs a second decision to widen "
                "to the rest of the fleet. Pre-approve widening now (the canary gate still has "
                "to pass first), or hold and decide after seeing the canary?"
            ),
            "expects": "pre-approve/hold",
            "blocking": True,
        }
    )
    return out


def cmd_ask(kit):
    m = load(kit)
    qs = questions(m)
    print(f"# Interview for {m['id']} ({len(qs)} questions, answer all at once)")
    print(f"# manifest fingerprint: {manifest_fingerprint(kit)}")
    print()
    for i, q in enumerate(qs, 1):
        print(f"{i}. [{q['topic']}] {q['ask']}")
        print(f"   id={q['id']}  expects={q['expects']}")
        print()
    print('# Answer by writing answers.json: {"<id>": "<answer>", ...}')
    print(f"# Then: interview-once.py record {kit} answers.json")
    return 0


def cmd_record(kit, answers_path):
    m = load(kit)
    qs = questions(m)
    with open(answers_path) as handle:
        answers = json.load(handle)

    missing = [
        q["id"]
        for q in qs
        if q["blocking"] and not str(answers.get(q["id"], "")).strip()
    ]
    if missing:
        print(
            "REFUSED: every blocking question must be answered before the run starts.\n  "
            + "\n  ".join(missing),
            file=sys.stderr,
        )
        return 2

    rejected = [
        q["id"]
        for q in qs
        if q["expects"] == "yes/no"
        and str(answers.get(q["id"], "")).strip().lower() not in ("yes", "y")
    ]
    if rejected:
        print(
            "STOP: these were not approved, so the run must not start:\n  "
            + "\n  ".join(rejected),
            file=sys.stderr,
        )
        return 3

    decision = {
        "kit_id": m["id"],
        "patch_sha": m["patch_sha"],
        "manifest_sha256": manifest_fingerprint(kit),
        "question_count": len(qs),
        "answers": {q["id"]: answers[q["id"]] for q in qs},
        "fleet_widening": str(answers.get("Q-fleet-widening", "hold")).strip().lower(),
    }
    path = os.path.join(kit, "DECISION.json")
    with open(path, "w") as handle:
        json.dump(decision, handle, indent=2)
        handle.write("\n")
    print(f"DECISION_RECORDED {path}")
    print(f"  fleet_widening = {decision['fleet_widening']}")
    if decision["fleet_widening"] == "pre-approve":
        print(f"  export OC_PATCH_FLEET_APPROVED={m['id']}:{m['patch_sha']}")
    return 0


def cmd_check(kit):
    path = os.path.join(kit, "DECISION.json")
    if not os.path.exists(path):
        print(
            "NO_DECISION: run `interview-once.py ask` then `record` first",
            file=sys.stderr,
        )
        return 2
    with open(path) as handle:
        d = json.load(handle)
    m = load(kit)
    live = manifest_fingerprint(kit)
    if d.get("manifest_sha256") != live:
        print(
            "STALE_DECISION: the manifest changed after these answers were given "
            f"({d.get('manifest_sha256', '?')[:12]} -> {live[:12]}). Re-run the interview.",
            file=sys.stderr,
        )
        return 3
    qs = questions(m)
    unanswered = [q["id"] for q in qs if q["id"] not in d.get("answers", {})]
    if unanswered:
        print(
            "INCOMPLETE_DECISION: new questions appeared since the interview:\n  "
            + "\n  ".join(unanswered),
            file=sys.stderr,
        )
        return 4
    print(f"DECISION_OK {m['id']} ({len(qs)} questions answered)")
    return 0


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    mode, kit = argv[1], argv[2]
    if mode == "ask":
        return cmd_ask(kit)
    if mode == "record":
        if len(argv) != 4:
            print(
                "usage: interview-once.py record <kit> <answers.json>", file=sys.stderr
            )
            return 2
        return cmd_record(kit, argv[3])
    if mode == "check":
        return cmd_check(kit)
    print(f"unknown mode {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
