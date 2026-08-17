#!/usr/bin/env python3
"""Derive, record, and validate one-time operator decisions for a patch kit."""

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse


SCHEMA_VERSION = 1
DECISION_NAME = "DECISION.json"
RESOLVE_USAGE_PLAN = "resolve-from-usage-plan"
CREDENTIAL_KEY = re.compile(
    r"(^|[._-])(token|secret|password|passwd|api[_-]?key|credential)([._-]|$)",
    re.IGNORECASE,
)


def fail(message):
    raise ValueError(message)


def load_json(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"{label} not found: {path}")
    except json.JSONDecodeError as exc:
        fail(f"{label} is not valid JSON: {path}: {exc}")
    return value


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def manifest_fingerprint(kit):
    manifest_path = kit / "manifest.json"
    try:
        return sha256_bytes(manifest_path.read_bytes())
    except FileNotFoundError:
        fail(f"manifest not found: {manifest_path}")


def answers_fingerprint(answers):
    encoded = json.dumps(
        answers, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256_bytes(encoded)


def path_value(value, dotted):
    for key in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def environment_defaults(env_path):
    if env_path is None:
        return {}
    env = load_json(env_path, "environment file")
    if not isinstance(env, dict):
        fail("environment file must contain a JSON object")
    return {
        "url": next(
            (
                candidate
                for candidate in (
                    path_value(env, "control_plane_url"),
                    path_value(env, "inputs.control_plane_url"),
                    path_value(env, "control_plane_api.url"),
                )
                if isinstance(candidate, str) and candidate
            ),
            None,
        ),
        "headers": next(
            (
                candidate
                for candidate in (
                    path_value(env, "probe_headers_file"),
                    path_value(env, "inputs.probe_headers_file"),
                )
                if isinstance(candidate, str) and candidate
            ),
            None,
        ),
        "ami": path_value(env, "new_ami_id"),
        "canary": path_value(env, "canary_instance_id"),
    }


def operation_text(path, operation):
    fields = [
        path,
        operation.get("resource", ""),
        operation.get("apply_cli", ""),
        operation.get("verify_cli", ""),
        operation.get("rollback_cli", ""),
    ]
    return " ".join(str(field) for field in fields).lower()


def is_route_operation(path, operation):
    text = operation_text(path, operation)
    return any(
        marker in text
        for marker in (
            "api gateway",
            "apigateway",
            "execute-api",
            "private api",
            "rest api",
        )
    )


def capabilities(kit, manifest):
    host_dir = kit / "host-scripts"
    has_data = (kit / "launch-template").exists() or (
        host_dir.is_dir() and any(host_dir.rglob("*.patched"))
    )
    has_control = (kit / "lambda" / "api").is_dir()
    has_routes = any(
        is_route_operation(path, operation)
        for path, record in (manifest.get("paths") or {}).items()
        for operation in record.get("operations") or []
    )
    return has_control, has_data, has_routes


def question(
    question_id,
    prompt,
    accepted,
    why,
    value_type,
    default=None,
    when_scopes=None,
):
    item = {
        "id": question_id,
        "prompt": prompt,
        "accepted": accepted,
        "default": default,
        "why": why,
        "type": value_type,
    }
    if when_scopes:
        item["when_scopes"] = when_scopes
    return item


def derive_questions(kit, manifest, env_path=None):
    defaults = environment_defaults(env_path)
    has_control, has_data, has_routes = capabilities(kit, manifest)
    manifest_questions = []

    for fix in manifest.get("fixes") or []:
        condition = fix.get("applies_when")
        if condition and condition != "always":
            fix_id = str(fix.get("id") or "unnamed")
            manifest_questions.append(
                question(
                    f"fix.{fix_id}.condition-holds",
                    f"Does this environment satisfy applies_when={condition!r} for {fix_id}?",
                    [True, False],
                    "A conditional fix must not be applied outside the environment it targets.",
                    "boolean",
                    False,
                )
            )

    for path, record in (manifest.get("paths") or {}).items():
        for operation in record.get("operations") or []:
            if operation.get("class") != "MANUAL_CLI_REVIEW":
                continue
            resource = str(operation.get("resource") or "unnamed resource")
            digest = sha256_bytes(f"{path}\0{resource}".encode("utf-8"))[:12]
            scope = "route" if is_route_operation(path, operation) else "data"
            manifest_questions.append(
                question(
                    f"manual.{scope}.{digest}",
                    f"Has the manual operation been reviewed for {path}: {resource}?",
                    [True, False],
                    "The generated CLI operation may cross a resource boundary that needs human review.",
                    "boolean",
                    False,
                )
            )

    # One authorization covers the whole lifecycle class, not one question per record.
    # Asking per verification produced 13 prompts on this kit whose answer is
    # necessarily the same — the decision is "may this environment host throwaway test
    # tenants at all", and splitting it only trains the operator to answer blind. The
    # ids stay visible in the prompt so the scope of the single answer is explicit.
    lifecycle_ids = sorted(
        str(verification.get("id") or "unnamed")
        for verification in manifest.get("verifications") or []
        if verification.get("phase") == "B-lifecycle"
    )
    if lifecycle_ids:
        manifest_questions.append(
            question(
                "verification.live-lifecycle",
                "May the lifecycle verifications create and delete throwaway test "
                f"tenants in this environment ({len(lifecycle_ids)} record(s): "
                f"{', '.join(lifecycle_ids)})?",
                [True, False],
                "These verifications exercise a live tenant lifecycle and must be authorized explicitly.",
                "boolean",
                False,
            )
        )

    operational = has_control or has_data or has_routes
    if not operational:
        return manifest_questions

    scopes = ["control-plane-only"]
    if has_data:
        scopes.append("control-plane-and-data-plane")
    if has_routes:
        scopes.append(
            "control-plane-data-plane-and-routes"
            if has_data
            else "control-plane-and-routes"
        )
    data_scopes = [value for value in scopes if "data-plane" in value]

    fixed = [
        question(
            "environment.control-plane-url",
            "What is the control-plane base URL used by the real client?",
            "An http:// or https:// URL without credentials, query, or fragment.",
            "Discovery confirms the API from this URL and authenticated route probes.",
            "url",
            defaults.get("url"),
        ),
        question(
            "environment.probe-auth",
            "How should discovery authenticate its probes?",
            f"A path to a JSON headers file, or {RESOLVE_USAGE_PLAN!r}.",
            "A usage-plan key is written only to a mode-0600 temporary headers file "
            "that is removed when the run exits; it is never written to DECISION.json, "
            "the receipt, or logs.",
            "auth",
            defaults.get("headers"),
        ),
    ]
    if len(scopes) > 1:
        fixed.append(
            question(
                "environment.scope",
                "Which cumulative patch scope should run?",
                scopes,
                "The scope decides which mutating phases are eligible to run.",
                "enum",
                "control-plane-only",
            )
        )
    if has_data:
        fixed.extend(
            [
                question(
                    "data-plane.ami-id",
                    "Which AMI id should the launch-template wave promote?",
                    "An AMI id such as ami-0123456789abcdef0.",
                    "The data-plane apply phase refuses to create a launch-template version without it.",
                    "ami",
                    defaults.get("ami"),
                    data_scopes,
                ),
                question(
                    "data-plane.canary-instance-id",
                    "Which canary instance id is expected, or should the canary phase select it automatically?",
                    "An EC2 instance id such as i-0123456789abcdef0, or 'auto'.",
                    "The receipt must identify whether canary selection was delegated to the existing phase.",
                    "canary",
                    defaults.get("canary") or "auto",
                    data_scopes,
                ),
                question(
                    "data-plane.allow-base-drift",
                    "May bootstrap DRIFT proceed with --allow-base-drift?",
                    [True, False],
                    "Proceeding rewrites an unrecognized in-service bootstrap lineage.",
                    "boolean",
                    False,
                    data_scopes,
                ),
            ]
        )
    return fixed + manifest_questions


def active_questions(questions, answers):
    scope = answers.get("environment.scope", "control-plane-only")
    return [
        item
        for item in questions
        if not item.get("when_scopes") or scope in item["when_scopes"]
    ]


def entropy(value):
    counts = {char: value.count(char) for char in set(value)}
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def looks_like_secret(value):
    if len(value) < 32 or any(char.isspace() for char in value):
        return False
    if value.startswith(("http://", "https://", "/", "./", "../", "~", "ami-", "i-")):
        return False
    if "/" in value or "\\" in value:
        return False
    return entropy(value) >= 4.0 and len(set(value)) >= 12


def validate_headers_file(path):
    headers = load_json(path, "probe headers file")
    if not isinstance(headers, dict):
        fail("probe headers file must contain a JSON object")
    for name, value in headers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", name):
            fail("probe headers file contains an invalid header name")
        if not isinstance(value, str) or re.search(r"[\r\n\t]", value):
            fail("probe headers file values must be safe strings")


def validate_value(item, value, answer_base):
    kind = item["type"]
    if kind == "boolean":
        if type(value) is not bool:
            fail(f"{item['id']} must be true or false")
        return value
    if not isinstance(value, str) or not value:
        fail(f"{item['id']} must be a non-empty string")
    if kind == "enum":
        if value not in item["accepted"]:
            fail(f"{item['id']} must be one of: {', '.join(item['accepted'])}")
    elif kind == "url":
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            fail(f"{item['id']} must be a base http(s) URL without credentials or query data")
    elif kind == "auth":
        if value == RESOLVE_USAGE_PLAN:
            return value
        path = pathlib.Path(value).expanduser()
        if not path.is_absolute():
            path = answer_base / path
        path = path.resolve()
        validate_headers_file(path)
        value = str(path)
    elif kind == "ami":
        if not re.fullmatch(r"ami-[0-9A-Fa-f]+", value):
            fail(f"{item['id']} must be an AMI id")
    elif kind == "canary":
        if value != "auto" and not re.fullmatch(r"i-[0-9A-Fa-f]+", value):
            fail(f"{item['id']} must be an instance id or 'auto'")
    if kind not in ("url", "auth", "ami", "canary") and looks_like_secret(value):
        fail(f"{item['id']} looks like a credential; pass a file path instead")
    return value


def validate_answers(questions, raw_answers, answer_base):
    if not isinstance(raw_answers, dict):
        fail("answers file must contain one JSON object keyed by question id")
    for key in raw_answers:
        if not isinstance(key, str):
            fail("every answer key must be a string")
        if CREDENTIAL_KEY.search(key):
            fail(f"answer key {key!r} looks credential-bearing; pass a headers file path instead")

    possible = {item["id"]: item for item in questions}
    unknown = sorted(set(raw_answers) - set(possible))
    if unknown:
        fail("unknown answer key(s): " + ", ".join(unknown))

    provisional = dict(raw_answers)
    active = active_questions(questions, provisional)
    active_ids = {item["id"] for item in active}
    supplied = {key for key, value in raw_answers.items() if value is not None}
    missing = sorted(active_ids - supplied)
    extra = sorted(supplied - active_ids)
    if missing:
        fail("missing answer(s): " + ", ".join(missing))
    if extra:
        fail("answer(s) are out of scope for the selected scope: " + ", ".join(extra))

    normalized = {}
    for item in active:
        normalized[item["id"]] = validate_value(
            item, raw_answers[item["id"]], answer_base
        )
    return normalized, active


def format_default(value):
    if value is None:
        return "<none>"
    return json.dumps(value, ensure_ascii=True)


def ask_mode(kit, env_path):
    manifest = load_json(kit / "manifest.json", "manifest")
    questions = derive_questions(kit, manifest, env_path)
    for item in questions:
        print(f"[{item['id']}]")
        print(f"Prompt: {item['prompt']}")
        print(f"Accepted: {json.dumps(item['accepted'], ensure_ascii=True)}")
        print(f"Default: {format_default(item['default'])}")
        if item.get("when_scopes"):
            print("When: " + ", ".join(item["when_scopes"]))
        print(f"Why: {item['why']}")
        print()
    print("Answers skeleton:")
    print(json.dumps({item["id"]: None for item in questions}, indent=2, sort_keys=True))


def parse_interactive(item):
    while True:
        suffix = "" if item["default"] is None else f" [{format_default(item['default'])}]"
        try:
            raw = input(f"{item['id']}: {item['prompt']}{suffix}\n> ").strip()
        except EOFError:
            fail("interactive interview ended before every question was answered")
        if not raw and item["default"] is not None:
            return item["default"]
        if item["type"] == "boolean":
            lowered = raw.lower()
            if lowered in ("true", "yes", "y"):
                return True
            if lowered in ("false", "no", "n"):
                return False
            print("Enter true or false.", file=sys.stderr)
            continue
        return raw


def interactive_answers(questions):
    answers = {}
    for item in questions:
        if item.get("when_scopes"):
            scope = answers.get("environment.scope", "control-plane-only")
            if scope not in item["when_scopes"]:
                continue
        answers[item["id"]] = parse_interactive(item)
    return answers


def write_decision(kit, answers, active):
    decision = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "kit_fingerprint": manifest_fingerprint(kit),
        "answers_fingerprint": answers_fingerprint(answers),
        "question_ids": [item["id"] for item in active],
        "answers": answers,
    }
    target = kit / DECISION_NAME
    fd, temporary = tempfile.mkstemp(prefix=".decision-", suffix=".json", dir=kit)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(decision, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(target)


def record_mode(kit, answers_path, env_path):
    manifest = load_json(kit / "manifest.json", "manifest")
    questions = derive_questions(kit, manifest, env_path)
    if answers_path is None:
        if not sys.stdin.isatty():
            fail("record needs an answers file when stdin is not a TTY")
        raw_answers = interactive_answers(questions)
        answer_base = pathlib.Path.cwd()
    else:
        answers_path = answers_path.expanduser().resolve()
        raw_answers = load_json(answers_path, "answers file")
        answer_base = answers_path.parent
    answers, active = validate_answers(questions, raw_answers, answer_base)
    write_decision(kit, answers, active)


def check_mode(kit):
    manifest = load_json(kit / "manifest.json", "manifest")
    decision = load_json(kit / DECISION_NAME, "decision")
    if not isinstance(decision, dict):
        fail("decision must contain a JSON object")
    if decision.get("schema_version") != SCHEMA_VERSION:
        fail("decision schema version is unsupported")
    if decision.get("kit_fingerprint") != manifest_fingerprint(kit):
        fail("manifest changed after the interview; record a new decision")
    answers = decision.get("answers")
    questions = derive_questions(kit, manifest)
    normalized, active = validate_answers(questions, answers, kit)
    if normalized != answers:
        fail("decision answers are not in canonical validated form")
    expected_ids = [item["id"] for item in active]
    if decision.get("question_ids") != expected_ids:
        fail("decision does not cover the current derived question set")
    if decision.get("answers_fingerprint") != answers_fingerprint(answers):
        fail("decision answers fingerprint does not match its answers")
    print(f"PASS: {kit / DECISION_NAME}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("ask", "record"):
        subparser = subparsers.add_parser(mode)
        subparser.add_argument("kit_dir", type=pathlib.Path)
        if mode == "record":
            subparser.add_argument("answers_json", type=pathlib.Path, nargs="?")
        subparser.add_argument("--env", dest="env_json", type=pathlib.Path)
    check = subparsers.add_parser("check")
    check.add_argument("kit_dir", type=pathlib.Path)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        kit = args.kit_dir.expanduser().resolve()
        if not kit.is_dir():
            fail(f"kit directory not found: {kit}")
        env_path = getattr(args, "env_json", None)
        if env_path is not None:
            env_path = env_path.expanduser().resolve()
        if args.mode == "ask":
            ask_mode(kit, env_path)
        elif args.mode == "record":
            record_mode(kit, args.answers_json, env_path)
        else:
            check_mode(kit)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
