#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compile a control-plane Lambda patch into a self-contained Bash apply/verify/rollback.

Why the overlay, not a prebuilt zip: the live function is arm64 python3.12 with native
wheels (cryptography/PyJWT/powertools). A zip built here would freeze THIS machine's
dependency versions onto the customer's function — an unrequested change, and fragile across
platforms. So the recipe downloads the live package, deletes only the first-party source
dirs, overlays the patched source, and re-zips. The platform-correct deps come from the
customer's own package. This is only valid when the patch did not touch requirements.txt;
the generator refuses otherwise.

Why the alias matters: `update-function-code` changes `$LATEST` immediately but leaves the alias
where it was, so alias-bound callers keep running the old version until the alias moves. The
recipe verifies `$LATEST` FIRST, and only then publishes a version and moves the alias — and
rollback moves both back.

What that does NOT cover, measured rather than assumed: a caller bound to the UNQUALIFIED
function ARN sees the new code the instant `$LATEST` is updated, before the verify. On the
Singapore testbed the private REST API integrates `ANY /` and `ANY /{proxy+}` that way, i.e. all
of its traffic, and an SQS event source mapping can be bound the same way. So the alias is a gate
for alias-bound callers only. `preflight-once.sh` and `patch-plan.sh` enumerate the unqualified
routes for the target function and print them, so an operator approves the rollout knowing which
of the two applies to their environment instead of trusting a protection that may not be there.
"""

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _q(v):
    return shlex.quote(str(v))


def _git_blob(repo, ref, path):
    r = subprocess.run(
        ["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True
    )
    return r.stdout if r.returncode == 0 else None


def lambda_recipe_id(function_name):
    return "fn-" + hashlib.sha256(function_name.encode()).hexdigest()[:10]


def _policy_name(manifest, fn):
    identity = "\0".join(
        (manifest["id"], manifest["patch_sha"], fn["function_name"])
    )
    return "oc-patch-lambda-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _validate_configuration(fn):
    fixed = fn.get("environment_updates") or {}
    generated = fn.get("generated_environment") or {}
    tables = fn.get("iam_read_tables") or []
    key_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_]+$")
    table_pattern = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")

    if not isinstance(fixed, dict) or not all(
        isinstance(key, str)
        and key_pattern.fullmatch(key)
        and isinstance(value, str)
        for key, value in fixed.items()
    ):
        raise SystemExit(
            "lambda_functions[0].environment_updates must map valid Lambda "
            "environment keys to string values"
        )
    if not isinstance(generated, dict) or not all(
        isinstance(key, str)
        and key_pattern.fullmatch(key)
        and value == "random_base64_32"
        for key, value in generated.items()
    ):
        raise SystemExit(
            "lambda_functions[0].generated_environment only supports "
            "{KEY: 'random_base64_32'}"
        )
    overlap = sorted(set(fixed) & set(generated))
    if overlap:
        raise SystemExit(
            "environment_updates and generated_environment overlap: "
            + ", ".join(overlap)
        )
    if (
        not isinstance(tables, list)
        or len(tables) != len(set(tables))
        or not all(
            isinstance(table, str) and table_pattern.fullmatch(table)
            for table in tables
        )
    ):
        raise SystemExit(
            "lambda_functions[0].iam_read_tables must contain unique DynamoDB "
            "table names"
        )
    rollback_payload = "rollback_verify_payload" in fn
    rollback_expect = "rollback_verify_expect" in fn
    if rollback_payload != rollback_expect:
        raise SystemExit(
            "lambda_functions[0].rollback_verify_payload and "
            "rollback_verify_expect must be declared together"
        )
    for key in ("verify_payload", "verify_expect"):
        value = fn.get(key)
        if value is not None and not isinstance(value, dict):
            raise SystemExit(f"lambda_functions[0].{key} must be an object")
    for key in ("rollback_verify_payload", "rollback_verify_expect"):
        value = fn.get(key)
        if value is not None and not isinstance(value, dict):
            raise SystemExit(f"lambda_functions[0].{key} must be an object")
    target_account = fn.get("target_account")
    if not isinstance(target_account, str) or not re.fullmatch(
        r"[0-9]{12}", target_account
    ):
        if target_account is None or target_account == "":
            raise SystemExit(
                "lambda_functions[0].target_account is required: the run checks "
                "the live STS caller against the kit target"
            )
        raise SystemExit(
            "lambda_functions[0].target_account must be a 12-digit account id"
        )
    target_region = fn.get("target_region")
    if not isinstance(target_region, str) or not re.fullmatch(
        r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", target_region
    ):
        if target_region is None:
            raise SystemExit(
                "lambda_functions[0].target_region is required: the run checks "
                "the runtime region against the kit target"
            )
        raise SystemExit(
            "lambda_functions[0].target_region must be a valid AWS region code"
        )


def _state_helper():
    return r'''#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote


def load(path):
    with open(path) as handle:
        return json.load(handle)


def write_new(path, value):
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return False
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    return True


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_sha256(path):
    digest = hashlib.sha256(Path(path).read_bytes()).digest()
    return base64.b64encode(digest).decode()


def copy_new(source, destination):
    destination = Path(destination)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fail(f"incomplete backup state: {destination.name} already exists", 44)
    with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())


def valid_code_sha(value):
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def backup_metadata(state):
    meta = load(Path(state) / "backup.meta")
    required = {
        "schema_version",
        "code_sha256",
        "alias",
        "anchor_version",
        "esm",
        "backup_zip_sha256",
        "rollback_probe",
    }
    if set(meta) != required or meta["schema_version"] != 1:
        fail("incomplete backup.meta: fields do not match schema", 44)
    alias = meta["alias"]
    probe = meta["rollback_probe"]
    if (
        not valid_code_sha(meta["code_sha256"])
        or not isinstance(alias, dict)
        or set(alias) != {"name", "version"}
        or not all(isinstance(alias[key], str) and alias[key] for key in alias)
        or not isinstance(meta["anchor_version"], str)
        or not meta["anchor_version"]
        or not isinstance(meta["esm"], list)
        or not all(isinstance(item, str) and item for item in meta["esm"])
        or not re.fullmatch(r"[0-9a-f]{64}", meta["backup_zip_sha256"])
        or not isinstance(probe, dict)
        or set(probe)
        != {
            "payload_sha256",
            "latest_expect_sha256",
            "alias_expect_sha256",
            "comparison",
        }
        or probe["comparison"] not in {"exact", "subset"}
        or not all(
            re.fullmatch(r"[0-9a-f]{64}", probe[key])
            for key in (
                "payload_sha256",
                "latest_expect_sha256",
                "alias_expect_sha256",
            )
        )
    ):
        fail("incomplete backup.meta: invalid field value", 44)
    return meta


def fail(message, code=40):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def variables(config):
    return ((config.get("Environment") or {}).get("Variables") or {}).copy()


def touched(spec):
    return sorted(
        set(spec.get("environment_updates") or {})
        | set(spec.get("generated_environment") or {})
    )


def original_matches(current, backup):
    for key, saved in backup.items():
        if saved["present"] != (key in current):
            return False
        if saved["present"] and current[key] != saved["value"]:
            return False
    return True


def desired_matches(current, spec, state):
    for key, value in (spec.get("environment_updates") or {}).items():
        if current.get(key) != value:
            return False
    hashes = load(state / "generated-hashes.json")
    for key, want in hashes.items():
        if key not in current:
            return False
        got = hashlib.sha256(current[key].encode()).hexdigest()
        if got != want:
            return False
    return True


def normalize_policy(path):
    value = load(path)
    if isinstance(value, dict) and set(value) == {"PolicyDocument"}:
        value = value["PolicyDocument"]
    if isinstance(value, str):
        value = json.loads(unquote(value))
    return value


NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
DIR_FLAGS = os.O_RDONLY | DIRECTORY | NOFOLLOW


def archive_parts(name):
    if "\\" in name:
        fail(f"unsafe zip backslash in path: {name}", 49)
    if name.startswith("/") or re.match(r"^[A-Za-z]:/", name):
        fail(f"unsafe zip absolute path: {name}", 49)
    trimmed = name[:-1] if name.endswith("/") else name
    raw = trimmed.split("/")
    if not trimmed or any(part in ("", ".") for part in raw):
        fail(f"unsafe zip ambiguous path: {name}", 49)
    if ".." in raw:
        fail(f"unsafe zip parent traversal: {name}", 49)
    path = PurePosixPath(trimmed)
    if path.is_absolute():
        fail(f"unsafe zip absolute path: {name}", 49)
    return tuple(path.parts)


def scan_archive(package):
    entries = []
    kinds = {}
    for info in package.infolist():
        parts = archive_parts(info.filename)
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type == stat.S_IFLNK:
            fail(f"unsafe zip symlink: {info.filename}", 49)
        if info.is_dir():
            if file_type not in (0, stat.S_IFDIR):
                fail(f"unsafe zip special file: {info.filename}", 49)
            kind = "dir"
        else:
            if file_type not in (0, stat.S_IFREG):
                fail(f"unsafe zip special file: {info.filename}", 49)
            kind = "file"
        if parts in kinds:
            fail(f"unsafe zip duplicate path: {info.filename}", 49)
        for depth in range(1, len(parts)):
            if kinds.get(parts[:depth]) == "file":
                fail(f"unsafe zip parent/child conflict: {info.filename}", 49)
        if kind == "file" and any(
            len(other) > len(parts) and other[: len(parts)] == parts
            for other in kinds
        ):
            fail(f"unsafe zip parent/child conflict: {info.filename}", 49)
        kinds[parts] = kind
        entries.append((info, parts, kind, mode & 0o777))
    return entries


def open_directory_path(path):
    path = Path(path).absolute()
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as error:
                fail(f"unsafe symlink or non-directory path component: {path}: {error}", 49)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def create_root(path):
    path = Path(path).absolute()
    parent_fd = open_directory_path(path.parent)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            fail(f"unsafe extraction root already exists: {path}", 49)
        try:
            return os.open(path.name, DIR_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            fail(f"unsafe symlink extraction root: {path}: {error}", 49)
    finally:
        os.close(parent_fd)


def descend(root_fd, parts, create):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as error:
                fail(
                    f"unsafe symlink or non-directory path component: "
                    f"{'/'.join(parts)}: {error}",
                    49,
                )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def secure_read(root_fd, parts):
    try:
        parent_fd = descend(root_fd, parts[:-1], False)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            fail(f"unsafe symlink or special overlay path: {'/'.join(parts)}: {error}", 49)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                fail(f"unsafe special overlay file: {'/'.join(parts)}", 49)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                return handle.read()
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def secure_write(root_fd, parts, content, mode=0o600, exclusive=False):
    parent_fd = descend(root_fd, parts[:-1], True)
    flags = os.O_WRONLY | os.O_CREAT | NOFOLLOW
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    try:
        try:
            fd = os.open(parts[-1], flags, mode or 0o600, dir_fd=parent_fd)
        except OSError as error:
            fail(f"unsafe symlink or special write path: {'/'.join(parts)}: {error}", 49)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                fail(f"unsafe special write file: {'/'.join(parts)}", 49)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                if hasattr(content, "read"):
                    shutil.copyfileobj(content, handle)
                else:
                    handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.fchmod(fd, mode or 0o600)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def safe_extract(archive, root):
    try:
        package = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        fail(f"unsafe or unreadable live zip: {error}", 49)
    with package:
        entries = scan_archive(package)
        root_fd = create_root(root)
        try:
            for _, parts, kind, mode in entries:
                if kind == "dir":
                    directory_fd = descend(root_fd, parts, True)
                    try:
                        os.fchmod(directory_fd, mode or 0o700)
                    finally:
                        os.close(directory_fd)
            for info, parts, kind, mode in entries:
                if kind == "file":
                    try:
                        with package.open(info) as source:
                            secure_write(
                                root_fd,
                                parts,
                                source,
                                mode or 0o600,
                                exclusive=True,
                            )
                    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                        fail(f"unsafe or unreadable live zip member: {info.filename}: {error}", 49)
        finally:
            os.close(root_fd)


def apply_overlay(root, payload_path):
    payload = load(payload_path)
    base = payload["base_hashes"]
    patched = payload["patch_hashes"]
    sources = payload["sources"]
    root_fd = open_directory_path(root)
    try:
        for rel, want in base.items():
            parts = archive_parts(rel)
            content = secure_read(root_fd, parts)
            if content is None:
                if want is None:
                    continue
                fail(f"DRIFT: {rel} is missing from the live package")
            got = hashlib.sha256(content).hexdigest()
            if got not in (want, patched.get(rel)):
                fail(
                    f"DRIFT: {rel} live hash {got[:12]} is neither the base this "
                    "patch was built against nor the patched content - refusing "
                    "to overwrite someone else's change"
                )
        for rel, encoded in sources.items():
            parts = archive_parts(rel)
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                fail(f"invalid base64 overlay source: {rel}", 49)
            secure_write(root_fd, parts, content)
    finally:
        os.close(root_fd)
    print(f"baseline verified and overlaid {len(sources)} file(s), deleted none")


command = sys.argv[1]

if command == "safe-extract":
    safe_extract(sys.argv[2], sys.argv[3])

elif command == "apply-overlay":
    apply_overlay(sys.argv[2], sys.argv[3])

elif command == "field":
    print(load(sys.argv[2])[sys.argv[3]])

elif command == "review-fingerprint":
    value = load(sys.argv[2]).get("kit_fingerprint")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("REVIEW.json has no valid final kit_fingerprint", 44)
    print(value)

elif command == "json-check":
    try:
        load(sys.argv[2])
    except (OSError, json.JSONDecodeError):
        fail("probe response is not valid JSON", 43)

elif command == "backup-create":
    state = Path(sys.argv[2])
    candidate_zip, latest_expect, alias_expect, payload = map(Path, sys.argv[3:7])
    code_sha, alias_name, alias_version, anchor, esm_raw, comparison = sys.argv[7:13]
    if code_sha256(candidate_zip) != code_sha:
        fail("backup zip CodeSha256 does not match the published anchor", 43)
    for path in (latest_expect, alias_expect, payload):
        try:
            load(path)
        except (OSError, json.JSONDecodeError):
            fail(f"backup probe file is not valid JSON: {path}", 43)
    copy_new(candidate_zip, state / "backup.zip")
    copy_new(latest_expect, state / "rollback-expect-latest.json")
    copy_new(alias_expect, state / "rollback-expect-alias.json")
    meta = {
        "schema_version": 1,
        "code_sha256": code_sha,
        "alias": {"name": alias_name, "version": alias_version},
        "anchor_version": anchor,
        "esm": sorted(filter(None, esm_raw.split())),
        "backup_zip_sha256": file_sha256(candidate_zip),
        "rollback_probe": {
            "payload_sha256": file_sha256(payload),
            "latest_expect_sha256": file_sha256(latest_expect),
            "alias_expect_sha256": file_sha256(alias_expect),
            "comparison": comparison,
        },
    }
    candidate_meta = state / f"backup.meta.candidate.{os.getpid()}"
    if not write_new(candidate_meta, meta):
        fail("could not create unique backup metadata candidate", 44)
    try:
        os.link(candidate_meta, state / "backup.meta")
    except FileExistsError:
        fail("backup.meta already exists; refusing to replace it", 44)
    os.replace(
        candidate_meta,
        state / "archive" / f"{candidate_meta.name}.{secrets.token_hex(4)}",
    )

elif command == "backup-validate":
    state, payload = Path(sys.argv[2]), Path(sys.argv[3])
    meta = backup_metadata(state)
    files = {
        "backup.zip": meta["backup_zip_sha256"],
        "rollback-expect-latest.json": meta["rollback_probe"][
            "latest_expect_sha256"
        ],
        "rollback-expect-alias.json": meta["rollback_probe"]["alias_expect_sha256"],
    }
    for name, expected in files.items():
        path = state / name
        if not path.is_file() or path.is_symlink() or file_sha256(path) != expected:
            fail(f"incomplete backup state: {name} is missing or hash-mismatched", 44)
    if (
        code_sha256(state / "backup.zip") != meta["code_sha256"]
        or file_sha256(payload) != meta["rollback_probe"]["payload_sha256"]
    ):
        fail("incomplete backup state: code or rollback payload hash mismatch", 44)
    for name in ("rollback-expect-latest.json", "rollback-expect-alias.json"):
        try:
            load(state / name)
        except (OSError, json.JSONDecodeError):
            fail(f"incomplete backup state: {name} is not valid JSON", 44)

elif command == "backup-field":
    meta = backup_metadata(sys.argv[2])
    field = sys.argv[3]
    if field == "alias_name":
        print(meta["alias"]["name"])
    elif field == "alias_version":
        print(meta["alias"]["version"])
    elif field == "comparison":
        print(meta["rollback_probe"]["comparison"])
    elif field == "esm":
        print("\n".join(meta["esm"]))
    else:
        print(meta[field])

elif command == "init":
    config, spec = load(sys.argv[2]), load(sys.argv[3])
    state = Path(sys.argv[4])
    account, region = sys.argv[5:7]
    keys = touched(spec)
    current = variables(config)
    if keys:
        backup = {
            key: {"present": key in current, "value": current.get(key)}
            for key in keys
        }
        write_new(state / "environment-backup.json", backup)
        merged_path = state / "merged-environment.json"
        if not merged_path.exists():
            merged = current.copy()
            merged.update(spec.get("environment_updates") or {})
            for key, generator in (spec.get("generated_environment") or {}).items():
                if generator != "random_base64_32":
                    fail("unsupported generated environment value for " + key, 2)
                merged[key] = base64.b64encode(secrets.token_bytes(32)).decode()
            write_new(merged_path, {"Variables": merged})
        merged = load(merged_path)["Variables"]
        hashes = {
            key: hashlib.sha256(merged[key].encode()).hexdigest()
            for key in (spec.get("generated_environment") or {})
        }
        write_new(state / "generated-hashes.json", hashes)
        if not desired_matches(merged, spec, state):
            fail("DRIFT: saved merged environment does not match this kit")

    tables = spec.get("iam_read_tables") or []
    if tables:
        role_arn = config["Role"]
        role = {"arn": role_arn, "name": role_arn.rsplit("/", 1)[-1]}
        role_path = state / "execution-role.json"
        write_new(role_path, role)
        if load(role_path) != role:
            fail("DRIFT: Lambda execution role changed since patch state was created")
        partition = role_arn.split(":", 2)[1]
        resources = []
        for table in sorted(tables):
            arn = f"arn:{partition}:dynamodb:{region}:{account}:table/{table}"
            resources.extend((arn, arn + "/index/*"))
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "PatchOwnedDynamoDbRead",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchGetItem",
                ],
                "Resource": resources,
            }],
        }
        write_new(state / "iam-policy.json", policy)

elif command == "env-state":
    config, spec, state = load(sys.argv[2]), load(sys.argv[3]), Path(sys.argv[4])
    current = variables(config)
    if desired_matches(current, spec, state):
        print("desired")
    elif original_matches(current, load(state / "environment-backup.json")):
        print("original")
    else:
        fail("DRIFT: a patch-owned Lambda environment key has an unexpected value")

elif command == "verify-env":
    config, spec, state = load(sys.argv[2]), load(sys.argv[3]), Path(sys.argv[4])
    expected = sys.argv[5]
    current = variables(config)
    matches = (
        desired_matches(current, spec, state)
        if expected == "desired"
        else original_matches(current, load(state / "environment-backup.json"))
    )
    if not matches:
        fail("DRIFT: Lambda environment does not match patch-owned " + expected + " state")

elif command == "merged-current":
    config, spec, state = load(sys.argv[2]), load(sys.argv[3]), Path(sys.argv[4])
    candidate = variables(config)
    desired = load(state / "merged-environment.json")["Variables"]
    for key in touched(spec):
        candidate[key] = desired[key]
    if candidate != desired:
        fail("DRIFT: unrelated Lambda environment changed before configuration update")

elif command == "rollback-env":
    config, state, output = load(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    restored = variables(config)
    for key, saved in load(state / "environment-backup.json").items():
        if saved["present"]:
            restored[key] = saved["value"]
        else:
            restored.pop(key, None)
    write_new(output, {"Variables": restored})

elif command == "role-name":
    print(load(Path(sys.argv[2]) / "execution-role.json")["name"])

elif command == "verify-role":
    config, state = load(sys.argv[2]), Path(sys.argv[3])
    if config.get("Role") != load(state / "execution-role.json")["arn"]:
        fail("DRIFT: Lambda execution role no longer matches the patch-owned IAM policy")

elif command == "compare-policy":
    if normalize_policy(sys.argv[2]) != load(sys.argv[3]):
        fail("CONFLICT: existing kit-owned IAM policy has different permissions", 49)

else:
    fail("unknown state helper command: " + command, 2)
'''


def _common(manifest, fn):
    has_environment = bool(
        fn.get("environment_updates") or fn.get("generated_environment")
    )
    has_iam = bool(fn.get("iam_read_tables"))
    return f"""\
ARTIFACT_ID={_q(manifest["id"])}
CONTENT_VERSION={_q(manifest["patch_sha"])}
FUNCTION={_q(fn["function_name"])}
ALIAS={_q(fn.get("alias") or "live")}
TARGET_ACCOUNT={_q(fn["target_account"])}
TARGET_REGION={_q(fn["target_region"])}
RESOURCE_ID={_q(lambda_recipe_id(fn["function_name"]))}
POLICY_NAME={_q(_policy_name(manifest, fn))}
HAS_ENVIRONMENT={int(has_environment)}
HAS_IAM={int(has_iam)}
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
OVERLAY_PAYLOAD_FILE="$SCRIPT_DIR/payload/overlay.json"
VERIFY_PAYLOAD_FILE="$SCRIPT_DIR/payload/verify-payload.json"
VERIFY_EXPECT_FILE="$SCRIPT_DIR/payload/verify-expect.json"
ROLLBACK_VERIFY_PAYLOAD_FILE="$SCRIPT_DIR/payload/rollback-verify-payload.json"
ROLLBACK_VERIFY_EXPECT_FILE="$SCRIPT_DIR/payload/rollback-verify-expect.json"
CONFIG_SPEC_FILE="$SCRIPT_DIR/payload/lambda-config.json"
STATE_HELPER="$SCRIPT_DIR/lambda-state.py"
REVIEW_RECEIPT="$SCRIPT_DIR/../../../REVIEW.json"

STATE_ROOT="${{OC_PATCH_STATE_ROOT:-${{HOME:-/tmp}}/.oc-patch-lambda}}"
# State is scoped by ACCOUNT and REGION as well as kit/version. Without that, running the same
# kit against two environments shares one backup.zip and one recorded alias version, so a
# rollback in env B could restore env A's code. The account is resolved below before use.
STATE_DIR=""  # set after ACCOUNT_ID is known
REGION="${{OC_PATCH_REGION:?OC_PATCH_REGION required}}"
[[ "$REGION" =~ ^[a-z]{{2}}(-[a-z0-9]+)+-[0-9]+$ ]] || {{
  echo "FATAL: runtime region is not a valid AWS region code: $REGION" >&2
  exit 3
}}
[[ "$REGION" == "$TARGET_REGION" ]] || {{
  echo "FATAL: runtime region $REGION != kit target $TARGET_REGION" >&2
  exit 3
}}
[[ -f "$STATE_HELPER" && -f "$REVIEW_RECEIPT" ]] || {{
  echo "FATAL: compiled helper or final REVIEW.json is missing" >&2
  exit 44
}}
KIT_FINGERPRINT="$(python3 "$STATE_HELPER" review-fingerprint "$REVIEW_RECEIPT")"
CONFIGURED_ACCOUNT="${{OC_PATCH_ACCOUNT:-}}"
if [[ -n "$CONFIGURED_ACCOUNT" && ! "$CONFIGURED_ACCOUNT" =~ ^[0-9]{{12}}$ ]]; then
  echo "FATAL: OC_PATCH_ACCOUNT is not a 12-digit account id" >&2
  exit 3
fi
STS_ACCOUNT="$(AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true \\
  aws sts get-caller-identity --region "$REGION" \\
  --query Account --output text)" || {{
  echo "FATAL: could not read the caller account" >&2
  exit 46
}}
[[ "$STS_ACCOUNT" =~ ^[0-9]{{12}}$ ]] || {{
  echo "FATAL: could not resolve the account id" >&2
  exit 3
}}
[[ "$STS_ACCOUNT" == "$TARGET_ACCOUNT" ]] || {{
  echo "FATAL: live STS account $STS_ACCOUNT != kit target $TARGET_ACCOUNT" >&2
  exit 3
}}
[[ -z "$CONFIGURED_ACCOUNT" || "$CONFIGURED_ACCOUNT" == "$STS_ACCOUNT" ]] || {{
  echo "FATAL: OC_PATCH_ACCOUNT $CONFIGURED_ACCOUNT != live STS account $STS_ACCOUNT" >&2
  exit 3
}}
ACCOUNT_ID="$STS_ACCOUNT"
STATE_DIR="${{STATE_ROOT}}/${{ACCOUNT_ID}}/${{REGION}}/${{ARTIFACT_ID}}/${{CONTENT_VERSION}}/${{KIT_FINGERPRINT}}/${{RESOURCE_ID}}"
umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
mkdir -p "$STATE_DIR/archive"
chmod 700 "$STATE_DIR/archive"
for payload_file in "$OVERLAY_PAYLOAD_FILE" "$VERIFY_PAYLOAD_FILE" "$VERIFY_EXPECT_FILE" \\
  "$ROLLBACK_VERIFY_PAYLOAD_FILE" "$ROLLBACK_VERIFY_EXPECT_FILE" \\
  "$CONFIG_SPEC_FILE" "$STATE_HELPER"; do
  [[ -f "$payload_file" ]] || {{
    echo "FATAL: compiled payload missing: $payload_file" >&2
    exit 2
  }}
done

archive_existing() {{
  local path="$1" target
  [[ -e "$path" || -L "$path" ]] || return 0
  target="$STATE_DIR/archive/$(basename "$path").$(date +%s).$$.${{RANDOM}}"
  mv "$path" "$target"
}}

write_marker() {{
  local path="$1" value="$2" tmp="${{1}}.$$.${{RANDOM}}.tmp"
  archive_existing "$path"
  printf '%s' "$value" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$path"
}}

capture_configuration() {{
  local out="$1" tmp="${{1}}.$$.${{RANDOM}}.tmp"
  archive_existing "$out"
  aws lambda get-function-configuration --region "$REGION" \\
    --function-name "$FUNCTION" --output json > "$tmp" || {{
      echo "FATAL: could not read Lambda configuration" >&2
      exit 46
    }}
  chmod 600 "$tmp"
  mv "$tmp" "$out"
}}

config_field() {{
  python3 "$STATE_HELPER" field "$1" "$2"
}}

backup_field() {{
  python3 "$STATE_HELPER" backup-field "$STATE_DIR" "$1"
}}

classify_aws_error() {{
  local error="$1"
  if grep -Eq 'PreconditionFailedException|ResourceNotFoundException' "$error"; then
    printf '40'
  elif grep -Eq 'TooManyRequestsException|ResourceConflictException|Throttl' "$error"; then
    printf '41'
  elif grep -Eq 'AccessDenied|InvalidParameter|ValidationException|InvalidZip' "$error"; then
    printf '49'
  else
    printf '42'
  fi
}}

file_code_sha() {{
  python3 - "$1" <<'PYEOF'
import base64, hashlib, pathlib, sys
print(base64.b64encode(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).digest()).decode())
PYEOF
}}

# CodeSha256 is base64 of the raw sha256 digest — not hex. Comparing the wrong encoding
# would silently never match and turn every verify into a false failure.
live_revision() {{
  local value
  value="$(aws lambda get-function-configuration --region "$REGION" \\
    --function-name "$1" --query RevisionId --output text)" || {{
      echo "FATAL: could not read Lambda RevisionId" >&2
      return 46
    }}
  printf '%s\\n' "$value"
}}

live_code_sha() {{
  local value
  value="$(aws lambda get-function-configuration --region "$REGION" \\
    --function-name "$1" --query CodeSha256 --output text)" || {{
      echo "FATAL: could not read Lambda CodeSha256" >&2
      return 46
    }}
  printf '%s\\n' "$value"
}}

qualified_code_sha() {{
  local qualifier="$1" value
  value="$(aws lambda get-function-configuration --region "$REGION" \\
    --function-name "$FUNCTION" --qualifier "$qualifier" \\
    --query CodeSha256 --output text)" || {{
      echo "FATAL: could not read published Lambda version $qualifier" >&2
      return 46
    }}
  printf '%s\\n' "$value"
}}

alias_version() {{
  local value
  value="$(aws lambda get-alias --region "$REGION" --function-name "$FUNCTION" \\
    --name "$ALIAS" --query FunctionVersion --output text)" || {{
      echo "FATAL: could not read Lambda alias version" >&2
      return 46
    }}
  printf '%s\\n' "$value"
}}

alias_revision() {{
  local value
  value="$(aws lambda get-alias --region "$REGION" --function-name "$FUNCTION" \\
    --name "$ALIAS" --query RevisionId --output text)" || {{
      echo "FATAL: could not read Lambda alias RevisionId" >&2
      return 46
    }}
  printf '%s\\n' "$value"
}}

# Every event source mapping bound to this function, and what it points at. An ESM on the
# unqualified ARN runs $LATEST; one on <fn>:<alias> follows the alias — so this is how you
# tell whether the async path will pick up an update-function-code immediately or only after
# the alias moves.
#
# Measured on a live account: `list-event-source-mappings --function-name <fn>` returns
# NOTHING for an ESM bound to <fn>:<alias>. The filter matches the qualified ARN, not the
# function, so relying on it reports "no async consumers" for a function that has them —
# a silent blind spot in exactly the case that matters. List and filter client-side instead.
esm_targets() {{
  aws lambda list-event-source-mappings --region "$REGION" \\
    --query "EventSourceMappings[?FunctionArn=='arn:aws:lambda:${{REGION}}:${{ACCOUNT_ID}}:function:${{FUNCTION}}' || starts_with(FunctionArn, 'arn:aws:lambda:${{REGION}}:${{ACCOUNT_ID}}:function:${{FUNCTION}}:')].FunctionArn" \\
    --output text || {{
      echo "FATAL: could not read Lambda event source mappings" >&2
      return 46
    }}
}}

build_overlay() {{
  local work="$1" zip_out="$2"
  archive_existing "$work"
  archive_existing "$zip_out"
  mkdir -p "$work"
  local url
  url="$(aws lambda get-function --region "$REGION" --function-name "$FUNCTION" \\
    --query Code.Location --output text)"
  curl -sSfL "$url" -o "$work/live.zip"
  python3 "$STATE_HELPER" safe-extract "$work/live.zip" "$work/pkg"
  # Before replacing anything, prove the live file is the one this patch was built against.
  # A file the customer edited by hand, or a package from a different revision, must NOT be
  # silently overwritten: that is the same "clobbering a hot change" failure this whole skill
  # exists to prevent, just one layer down.
  python3 "$STATE_HELPER" apply-overlay "$work/pkg" "$OVERLAY_PAYLOAD_FILE"
  # Overlay EXACT FILES. Unchanged first-party modules and third-party dependencies remain.
  # The large source document is a single hash-bound kit file, never argv or environment data.
  ( cd "$work/pkg" && zip -qr "$zip_out" . )
}}

backup_state() {{
  local present=0 name
  for name in backup.meta backup.zip rollback-expect-latest.json rollback-expect-alias.json; do
    [[ -e "$STATE_DIR/$name" ]] && present=$(( present + 1 ))
  done
  if [[ "$present" -eq 0 ]]; then
    printf 'absent\\n'
  elif [[ "$present" -eq 4 ]]; then
    printf 'ready\\n'
  else
    echo "FATAL: backup anchor is incomplete; refusing every live write" >&2
    exit 44
  fi
}}

validate_backup() {{
  local old_sha anchor saved_esm current_esm
  [[ "$(backup_state)" == "ready" ]] || {{
    echo "FATAL: backup anchor is missing" >&2
    exit 44
  }}
  python3 "$STATE_HELPER" backup-validate "$STATE_DIR" \
    "$ROLLBACK_VERIFY_PAYLOAD_FILE"
  old_sha="$(backup_field code_sha256)"
  anchor="$(backup_field anchor_version)"
  [[ "$(qualified_code_sha "$anchor")" == "$old_sha" ]] || {{
    echo "FATAL: immutable backup anchor no longer matches backup.meta" >&2
    exit 44
  }}
  [[ "$(backup_field alias_name)" == "$ALIAS" ]] || {{
    echo "FATAL: backup alias name does not match this recipe" >&2
    exit 44
  }}
  saved_esm="$(backup_field esm)"
  current_esm="$(esm_targets | tr '\\t' '\\n' | sed '/^$/d' | sort)"
  [[ "$current_esm" == "$saved_esm" ]] || {{
    echo "DRIFT: event source mappings changed after the backup anchor was captured" >&2
    exit 40
  }}
}}

prepare_configuration_state() {{
  local config="$STATE_DIR/current-configuration.json"
  capture_configuration "$config"
  python3 "$STATE_HELPER" init "$config" "$CONFIG_SPEC_FILE" "$STATE_DIR" \\
    "$ACCOUNT_ID" "$REGION"
}}

assert_execution_role() {{
  local config="$1"
  [[ "$HAS_IAM" == 1 ]] || return 0
  python3 "$STATE_HELPER" verify-role "$config" "$STATE_DIR"
}}

environment_state() {{
  [[ "$HAS_ENVIRONMENT" == 1 ]] || {{ printf 'desired'; return 0; }}
  local config="$STATE_DIR/current-configuration.json"
  capture_configuration "$config"
  assert_execution_role "$config"
  python3 "$STATE_HELPER" env-state "$config" "$CONFIG_SPEC_FILE" "$STATE_DIR"
}}

verify_environment() {{
  local expected="${{1:-desired}}" config="$STATE_DIR/current-configuration.json"
  [[ "$HAS_ENVIRONMENT" == 1 ]] || return 0
  for file in environment-backup.json merged-environment.json generated-hashes.json; do
    [[ -f "$STATE_DIR/$file" ]] || {{
      echo "FATAL: verify found no patch-owned environment state" >&2
      exit 44
    }}
  done
  capture_configuration "$config"
  assert_execution_role "$config"
  python3 "$STATE_HELPER" verify-env "$config" "$CONFIG_SPEC_FILE" "$STATE_DIR" "$expected"
}}

apply_environment() {{
  [[ "$HAS_ENVIRONMENT" == 1 ]] || return 0
  local config="$STATE_DIR/current-configuration.json" state rev error
  state="$(environment_state)"
  [[ "$state" == "desired" ]] && return 0
  python3 "$STATE_HELPER" merged-current "$config" "$CONFIG_SPEC_FILE" "$STATE_DIR"
  rev="$(config_field "$config" RevisionId)"
  error="$STATE_DIR/update-environment.err"
  archive_existing "$error"
  aws lambda update-function-configuration --region "$REGION" \\
    --function-name "$FUNCTION" \\
    --environment "file://$STATE_DIR/merged-environment.json" \\
    --revision-id "$rev" >/dev/null 2>"$error" || {{
      echo "FATAL: environment update rejected revision $rev; code and alias were untouched" >&2
      exit "$(classify_aws_error "$error")"
    }}
  aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION" || {{
    echo "FATAL: timed out waiting for Lambda environment update" >&2
    exit 42
  }}
  verify_environment desired
  echo "ENVIRONMENT_UPDATED keys verified"
}}

policy_status() {{
  local role current="$STATE_DIR/current-policy.json" error="$STATE_DIR/current-policy.err"
  [[ "$HAS_IAM" == 1 ]] || return 0
  role="$(python3 "$STATE_HELPER" role-name "$STATE_DIR")"
  archive_existing "$current"
  archive_existing "$error"
  if aws iam get-role-policy --role-name "$role" --policy-name "$POLICY_NAME" \\
      --query PolicyDocument --output json > "$current" 2> "$error"; then
    chmod 600 "$current" "$error"
    python3 "$STATE_HELPER" compare-policy "$current" "$STATE_DIR/iam-policy.json" ||
      return 49
    return 0
  fi
  chmod 600 "$current" "$error"
  if grep -q 'NoSuchEntity' "$error"; then
    return 1
  fi
  echo "FATAL: could not inspect kit-owned IAM policy" >&2
  sed -n '1,5p' "$error" >&2
  return 46
}}

preflight_policy() {{
  local rc
  [[ "$HAS_IAM" == 1 ]] || return 0
  if policy_status; then
    return 0
  else
    rc=$?
  fi
  [[ "$rc" == 1 ]] || exit "$rc"
}}

apply_policy() {{
  local role config="$STATE_DIR/current-configuration.json" rc
  local error="$STATE_DIR/put-role-policy.err"
  [[ "$HAS_IAM" == 1 ]] || return 0
  capture_configuration "$config"
  assert_execution_role "$config"
  if policy_status; then
    if [[ -f "$STATE_DIR/iam-policy.create-intent" &&
          ! -f "$STATE_DIR/iam-policy.created" ]]; then
      write_marker "$STATE_DIR/iam-policy.created" created
      archive_existing "$STATE_DIR/iam-policy.create-intent"
    fi
    return 0
  else
    rc=$?
  fi
  [[ "$rc" == 1 ]] || exit "$rc"
  role="$(python3 "$STATE_HELPER" role-name "$STATE_DIR")"
  [[ -f "$STATE_DIR/iam-policy.create-intent" ]] ||
    write_marker "$STATE_DIR/iam-policy.create-intent" pending
  archive_existing "$error"
  aws iam put-role-policy --role-name "$role" --policy-name "$POLICY_NAME" \\
    --policy-document "file://$STATE_DIR/iam-policy.json" >/dev/null 2>"$error" || {{
      sed -n '1,5p' "$error" >&2
      echo "FATAL: IAM policy creation failed; code and alias were untouched" >&2
      exit "$(classify_aws_error "$error")"
    }}
  write_marker "$STATE_DIR/iam-policy.created" created
  archive_existing "$STATE_DIR/iam-policy.create-intent"
  policy_status || {{
    echo "FATAL: IAM policy did not read back exactly after creation" >&2
    exit 49
  }}
  echo "IAM_POLICY_UPDATED name=$POLICY_NAME"
}}

verify_policy() {{
  local config="$STATE_DIR/current-configuration.json" rc
  [[ "$HAS_IAM" == 1 ]] || return 0
  [[ -f "$STATE_DIR/execution-role.json" && -f "$STATE_DIR/iam-policy.json" ]] || {{
    echo "FATAL: verify found no patch-owned IAM state" >&2
    exit 44
  }}
  capture_configuration "$config"
  assert_execution_role "$config"
  if policy_status; then
    return 0
  else
    rc=$?
  fi
  echo "DRIFT: kit-owned IAM policy is missing or unreadable" >&2
  exit "$rc"
}}

assert_probe_response() {{
  local out="$1" expect="$2" comparison="$3"
  [[ -s "$expect" ]] || return 0
  python3 - "$out" "$expect" "$comparison" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as handle:
    body = json.load(handle)
with open(sys.argv[2]) as handle:
    expect = json.load(handle)
if sys.argv[3] == "exact":
    matches = body == expect
else:
    matches = all(str(body.get(key)) == str(want) for key, want in expect.items())
if not matches:
    print(
        f"response assertion failed: got {{body!r}}, expected {{sys.argv[3]}} {{expect!r}}",
        file=sys.stderr,
    )
    raise SystemExit(43)
PYEOF
}}

capture_probe() {{
  local qualifier="$1" payload="$2" out="$3" rc
  archive_existing "$out"
  set +e
  rc="$(aws lambda invoke --region "$REGION" \
    --function-name "${{FUNCTION}}:${{qualifier}}" \
    --payload "file://$payload" --cli-binary-format raw-in-base64-out \
    --query FunctionError --output text "$out")"
  aws_rc=$?
  set -e
  if [[ "$aws_rc" -ne 0 ]]; then
    echo "FATAL: Lambda probe call failed or timed out" >&2
    return 42
  fi
  [[ "$rc" == "None" ]] || {{
    echo "FATAL: Lambda probe returned FunctionError=$rc" >&2
    return 43
  }}
  python3 "$STATE_HELPER" json-check "$out"
}}

invoke_checked() {{
  local qualifier="$1" payload="$2" expect="$3" comparison="$4" label="$5" out rc
  out="$STATE_DIR/invoke-${{label}}-${{qualifier//[^A-Za-z0-9]/_}}.json"
  capture_probe "$qualifier" "$payload" "$out" || return $?
  assert_probe_response "$out" "$expect" "$comparison" || return $?
}}

invoke_ok() {{
  # FunctionError=None is the signal, not a 200 body: on a private API a synthetic path
  # legitimately returns 404. This proves the new code LOADS and runs without raising.
  #
  # The payload SHAPE matters and is the author's responsibility: this handler reads
  # event["httpMethod"] (REST API v1). Feeding it an HTTP-API-v2 event produced
  # `KeyError: 'httpMethod'` on the real function — an "Unhandled" FunctionError that looks
  # exactly like a broken patch. The gate was right to stop; the probe was wrong. If
  # verify_payload is the wrong shape, every apply fails at a green build, so validate it
  # against the CURRENT function before shipping a kit.
  invoke_checked "$1" "$VERIFY_PAYLOAD_FILE" "$VERIFY_EXPECT_FILE" subset patch
}}

rollback_invoke_ok() {{
  local qualifier="$1" expect comparison
  comparison="$(backup_field comparison)"
  if [[ "$qualifier" == '$LATEST' ]]; then
    expect="$STATE_DIR/rollback-expect-latest.json"
  else
    expect="$STATE_DIR/rollback-expect-alias.json"
  fi
  invoke_checked "$qualifier" "$ROLLBACK_VERIFY_PAYLOAD_FILE" \
    "$expect" "$comparison" rollback
}}
"""


def _apply(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

for t in aws curl zip python3; do
  command -v "$t" >/dev/null || {{ echo "FATAL: need $t" >&2; exit 2; }}
done

work="$STATE_DIR/work"
new_zip="$STATE_DIR/patched.zip"

initial_backup_state="$(backup_state)"
if [[ "$initial_backup_state" == "ready" ]]; then
  validate_backup
fi
prepare_configuration_state
# A same-name policy with any different byte-level JSON meaning is someone else's state.
# Refuse before changing environment, code, or alias.
preflight_policy

# ---- backup, create-only ----------------------------------------------------------------
# The anchor is a PUBLISHED VERSION plus the downloaded zip: a version number cannot be
# overwritten later, so rollback has something immutable to point at.
if [[ "$initial_backup_state" == "absent" ]]; then
  config="$STATE_DIR/backup-anchor-configuration.json"
  capture_configuration "$config"
  assert_execution_role "$config"
  before_sha="$(config_field "$config" CodeSha256)"
  rev="$(config_field "$config" RevisionId)"
  before_alias="$(alias_version)"
  publish_error="$STATE_DIR/backup-publish.err"
  archive_existing "$publish_error"
  anchor="$(aws lambda publish-version --region "$REGION" --function-name "$FUNCTION" \\
    --description "oc-patch backup anchor $ARTIFACT_ID" \\
    --revision-id "$rev" --code-sha256 "$before_sha" \\
    --query Version --output text 2>"$publish_error")" || {{
      echo "FATAL: could not publish immutable backup anchor" >&2
      exit "$(classify_aws_error "$publish_error")"
    }}
  url="$(aws lambda get-function --region "$REGION" --function-name "$FUNCTION" \\
    --qualifier "$anchor" --query Code.Location --output text)" || {{
      echo "FATAL: could not read backup anchor download location" >&2
      exit 46
    }}
  candidate_zip="$STATE_DIR/backup-candidate.zip.$$.${{RANDOM}}"
  candidate_latest="$STATE_DIR/rollback-latest-candidate.$$.${{RANDOM}}.json"
  candidate_alias="$STATE_DIR/rollback-alias-candidate.$$.${{RANDOM}}.json"
  curl -sSfL "$url" -o "$candidate_zip" || {{
    echo "FATAL: could not download backup anchor package" >&2
    exit 42
  }}
  chmod 600 "$candidate_zip"
  capture_probe '$LATEST' "$ROLLBACK_VERIFY_PAYLOAD_FILE" "$candidate_latest" || exit $?
  capture_probe "$ALIAS" "$ROLLBACK_VERIFY_PAYLOAD_FILE" "$candidate_alias" || exit $?
  if [[ -s "$ROLLBACK_VERIFY_EXPECT_FILE" ]]; then
    assert_probe_response "$candidate_latest" "$ROLLBACK_VERIFY_EXPECT_FILE" subset ||
      exit $?
    assert_probe_response "$candidate_alias" "$ROLLBACK_VERIFY_EXPECT_FILE" subset ||
      exit $?
    latest_expect="$ROLLBACK_VERIFY_EXPECT_FILE"
    alias_expect="$ROLLBACK_VERIFY_EXPECT_FILE"
    comparison=subset
  else
    latest_expect="$candidate_latest"
    alias_expect="$candidate_alias"
    comparison=exact
  fi
  python3 "$STATE_HELPER" backup-create "$STATE_DIR" "$candidate_zip" \
    "$latest_expect" "$alias_expect" "$ROLLBACK_VERIFY_PAYLOAD_FILE" \
    "$before_sha" "$ALIAS" "$before_alias" "$anchor" "$(esm_targets)" "$comparison"
  archive_existing "$candidate_zip"
  archive_existing "$candidate_latest"
  archive_existing "$candidate_alias"
  validate_backup
  echo "BACKUP anchor_version=$anchor alias_was=$before_alias"
fi
backup_sha="$(backup_field code_sha256)"

# Lambda replaces the entire Variables map. apply_environment reads the complete current
# configuration, preserves unrelated keys, and binds the write to that read's RevisionId.
apply_environment
apply_policy

# ---- idempotent short-circuit -----------------------------------------------------------
if [[ -f "$STATE_DIR/applied.sha256" ]]; then
  want="$(cat "$STATE_DIR/applied.sha256")"
  live="$(live_code_sha "$FUNCTION")"
  if [[ "$live" != "$want" && "$live" != "$backup_sha" ]]; then
    echo "DRIFT: live CodeSha256 $live is neither the patch nor our backup" >&2
    exit 40
  fi
  # SKIP only on a COMPLETE previous run whose end state still holds. A run interrupted
  # between update-function-code and the alias move has applied.sha256 but no `complete`,
  # so it must resume rather than declare itself done.
  if [[ -f "$STATE_DIR/complete" && "$live" == "$want" &&
        "$(alias_version)" == "$(cat "$STATE_DIR/alias.version" 2>/dev/null || echo x)" ]]; then
    verify_environment desired
    verify_policy
    echo "SKIP $FUNCTION already at patched code and alias"
    exit 0
  fi
  echo "RESUME previous run did not finish; continuing"
fi

build_overlay "$work" "$new_zip"

# ---- update $LATEST only ---
# No --publish flag: it is a boolean switch (measured on the real CLI: `--publish false` is
# rejected as "Unknown options: false"), and not publishing is already the default. We publish
# explicitly further down, only after $LATEST has been verified.
# Sync traffic still runs the OLD version through the alias, so a bad build is contained.
# RevisionId is Lambda's own optimistic-concurrency token. Without it two runs (or a run and
# someone's console click) can interleave: the loser's code silently wins because the last
# write applies unconditionally. Read it immediately before writing and require it to still
# hold — a concurrent change makes this fail loudly instead of clobbering.
rev="$(live_revision "$FUNCTION")"
expected_sha="$(file_code_sha "$new_zip")"
update_error="$STATE_DIR/update-code.err"
archive_existing "$update_error"
response_sha="$(aws lambda update-function-code --region "$REGION" \
  --function-name "$FUNCTION" --zip-file "fileb://$new_zip" --revision-id "$rev" \
  --query CodeSha256 --output text 2>"$update_error")" || {{
  echo "FATAL: update-function-code failed" >&2
  exit "$(classify_aws_error "$update_error")"
}}
[[ "$response_sha" == "$expected_sha" ]] || {{
  echo "FATAL: update-function-code response hash $response_sha != expected $expected_sha" >&2
  exit 43
}}
aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION" || {{
  echo "FATAL: timed out waiting for Lambda code update" >&2
  exit 42
}}
live="$(live_code_sha "$FUNCTION")"
[[ "$live" == "$expected_sha" ]] || {{
  echo "DRIFT: live CodeSha256 $live != uploaded $expected_sha after wait" >&2
  exit 40
}}
write_marker "$STATE_DIR/applied.sha256" "$expected_sha"
echo "LATEST_UPDATED CodeSha256=$expected_sha"

# ---- verify $LATEST BEFORE moving the alias ----------------------------------------------
invoke_ok '$LATEST' || {{
  rc=$?
  echo "FATAL: \\$LATEST invoke reported a FunctionError — the alias was NOT moved, so" >&2
  echo "       alias-bound callers are still on the previous version. Callers bound to the" >&2
  echo "       unqualified ARN are already on this broken code: roll back now." >&2
  exit "$rc"
}}
echo "LATEST_VERIFIED FunctionError=None"

# ---- publish + move the alias ------------------------------------------------------------
# Publish the version we just verified, not "whatever $LATEST is now": binding the revision
# means a concurrent update between verify and publish fails rather than publishing code that
# was never verified.
rev="$(live_revision "$FUNCTION")"
publish_error="$STATE_DIR/publish-version.err"
archive_existing "$publish_error"
ver="$(aws lambda publish-version --region "$REGION" --function-name "$FUNCTION" \\
  --description "oc-patch $ARTIFACT_ID $CONTENT_VERSION" --revision-id "$rev" \\
  --code-sha256 "$expected_sha" --query Version --output text \\
  2>"$publish_error")" || {{
  sed -n '1,5p' "$publish_error" >&2
  echo "FATAL: publish-version rejected revision/code-sha — \\$LATEST changed after verify." >&2
  exit "$(classify_aws_error "$publish_error")"
}}
alias_error="$STATE_DIR/update-alias.err"
archive_existing "$alias_error"
aws lambda update-alias --region "$REGION" --function-name "$FUNCTION" \\
  --name "$ALIAS" --function-version "$ver" \\
  --revision-id "$(alias_revision)" >/dev/null 2>"$alias_error" || {{
    sed -n '1,5p' "$alias_error" >&2
    echo "FATAL: alias update failed" >&2
    exit "$(classify_aws_error "$alias_error")"
  }}
write_marker "$STATE_DIR/alias.version" "$ver"
echo "ALIAS_MOVED $ALIAS -> $ver"

invoke_ok "$ALIAS" || {{
  rc=$?
  echo "FATAL: alias invoke did not prove the patched effect" >&2
  exit "$rc"
}}
# The completion marker is written LAST, after both invoke paths were observed good. An
# interrupt anywhere earlier leaves it absent, so verify reports incomplete instead of
# passing on a half-applied function, and a rerun re-does the remaining work.
write_marker "$STATE_DIR/complete" "$expected_sha"
echo "APPLIED $FUNCTION version=$ver code=$expected_sha"
"""


def _verify(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

# Read-only. Both invoke paths are checked because they can disagree: API Gateway uses the
# alias while an SQS ESM may still be bound to $LATEST.
validate_backup
[[ -f "$STATE_DIR/applied.sha256" ]] || {{
  echo "FATAL: verify found no patch-owned anchor for $FUNCTION" >&2
  exit 44
}}
# An apply that never reached its final invoke leaves no `complete` marker. Reporting OK
# there would call a half-applied function live: $LATEST patched, the alias still old.
[[ -f "$STATE_DIR/complete" ]] || {{
  echo "INCOMPLETE: apply did not finish (no completion marker) — \\$LATEST may be patched" >&2
  echo "            while the alias is still on the previous version. Rerun apply." >&2
  exit 44
}}
[[ -f "$STATE_DIR/alias.version" ]] || {{
  echo "INCOMPLETE: no recorded alias version — the alias was never moved" >&2
  exit 44
}}
want="$(cat "$STATE_DIR/applied.sha256")"
live="$(live_code_sha "$FUNCTION")"
[[ "$live" == "$want" ]] || {{
  echo "DRIFT: \\$LATEST CodeSha256 $live != patch-owned $want" >&2
  exit 40
}}
want_ver="$(cat "$STATE_DIR/alias.version")"
now_ver="$(alias_version)"
[[ "$now_ver" == "$want_ver" ]] || {{
  echo "DRIFT: alias $ALIAS is on $now_ver, expected $want_ver" >&2
  exit 40
}}
verify_environment desired
verify_policy
invoke_ok "$ALIAS" || {{ rc=$?; echo "FATAL: alias effect is not verified" >&2; exit "$rc"; }}
invoke_ok '$LATEST' || {{ rc=$?; echo "FATAL: \\$LATEST effect is not verified" >&2; exit "$rc"; }}
echo "VERIFIED $FUNCTION code=$live alias=$(alias_version)"
# Audit which code each invoke path runs. An ESM on the UNQUALIFIED arn consumes $LATEST, so
# the async path is NOT gated by the alias: it picks up an update-function-code immediately,
# while API Gateway only moves when the alias does. That is what the CDK source produces
# (api_fn.add_event_source_mapping in deploy/lib/dispatch_infra.py), so it is the EXPECTED
# topology — the point of reporting it is that the two paths are gated separately, which is
# why rollback has to revert code AND alias.
unqualified=0
for arn in $(esm_targets); do
  if [[ "$arn" == "arn:aws:lambda:${{REGION}}:${{ACCOUNT_ID}}:function:${{FUNCTION}}" ]]; then
    unqualified=$(( unqualified + 1 ))
    echo "ESM_TARGET $arn -> \\$LATEST (async path NOT alias-gated)"
  else
    echo "ESM_TARGET $arn -> follows alias $ALIAS"
  fi
done
if [[ "$unqualified" -gt 0 ]]; then
  echo "NOTE $unqualified event source mapping(s) consume \\$LATEST rather than the alias." >&2
  echo "     Expected for this stack; it means async and sync are gated separately." >&2
fi
"""


def _rollback(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

validate_backup
old_sha="$(backup_field code_sha256)"
old_alias="$(backup_field alias_version)"
live="$(live_code_sha "$FUNCTION")"
now_alias="$(alias_version)"

if [[ -f "$STATE_DIR/applied.sha256" ]]; then
  want="$(cat "$STATE_DIR/applied.sha256")"
  [[ "$live" == "$want" || "$live" == "$old_sha" ]] || {{
    echo "DRIFT: rollback refused; live code is neither patch-owned nor the backup" >&2
    exit 40
  }}
else
  [[ "$live" == "$old_sha" ]] || {{
    echo "DRIFT: no patch code marker exists and live code is not the backup" >&2
    exit 40
  }}
fi

if [[ -f "$STATE_DIR/alias.version" ]]; then
  patch_alias="$(cat "$STATE_DIR/alias.version")"
  [[ "$now_alias" == "$patch_alias" || "$now_alias" == "$old_alias" ]] || {{
    echo "DRIFT: rollback refused; alias is neither patch-owned nor the backup" >&2
    exit 40
  }}
else
  [[ "$now_alias" == "$old_alias" ]] || {{
    echo "DRIFT: no patch alias marker exists and the alias changed" >&2
    exit 40
  }}
fi

env_state="original"
if [[ "$HAS_ENVIRONMENT" == 1 ]]; then
  for file in environment-backup.json merged-environment.json generated-hashes.json; do
    [[ -f "$STATE_DIR/$file" ]] || {{
      echo "FATAL: rollback found no patch-owned environment backup" >&2
      exit 44
    }}
  done
  env_state="$(environment_state)"
elif [[ "$HAS_IAM" == 1 ]]; then
  capture_configuration "$STATE_DIR/current-configuration.json"
  assert_execution_role "$STATE_DIR/current-configuration.json"
fi

iam_present=0
if [[ "$HAS_IAM" == 1 &&
      ( -f "$STATE_DIR/iam-policy.created" ||
        -f "$STATE_DIR/iam-policy.create-intent" ) ]]; then
  if policy_status; then
    iam_present=1
  else
    rc=$?
    [[ "$rc" == 1 ]] || exit "$rc"
  fi
fi

# All four surfaces passed their drift guards. Restore environment, IAM, code, and alias.
if [[ "$env_state" == "desired" ]]; then
  config="$STATE_DIR/current-configuration.json"
  rollback_environment="$STATE_DIR/rollback-environment.json"
  rollback_environment_error="$STATE_DIR/rollback-environment.err"
  archive_existing "$rollback_environment"
  archive_existing "$rollback_environment_error"
  python3 "$STATE_HELPER" rollback-env "$config" "$STATE_DIR" "$rollback_environment"
  rev="$(config_field "$config" RevisionId)"
  aws lambda update-function-configuration --region "$REGION" \\
    --function-name "$FUNCTION" --environment "file://$rollback_environment" \\
    --revision-id "$rev" >/dev/null 2>"$rollback_environment_error" || {{
      sed -n '1,5p' "$rollback_environment_error" >&2
      echo "FATAL: environment rollback rejected revision $rev" >&2
      exit "$(classify_aws_error "$rollback_environment_error")"
    }}
  aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION" || {{
    echo "FATAL: timed out waiting for Lambda environment rollback" >&2
    exit 42
  }}
  verify_environment original
fi

if [[ "$iam_present" == 1 ]]; then
  role="$(python3 "$STATE_HELPER" role-name "$STATE_DIR")"
  delete_policy_error="$STATE_DIR/delete-role-policy.err"
  archive_existing "$delete_policy_error"
  aws iam delete-role-policy --role-name "$role" --policy-name "$POLICY_NAME" \\
    >/dev/null 2>"$delete_policy_error" || {{
      sed -n '1,5p' "$delete_policy_error" >&2
      echo "FATAL: IAM policy deletion failed; code and alias were not rolled back" >&2
      exit "$(classify_aws_error "$delete_policy_error")"
    }}
  if policy_status; then
    echo "FATAL: IAM policy still exists after delete-role-policy" >&2
    exit 43
  else
    rc=$?
    [[ "$rc" == 1 ]] || exit "$rc"
  fi
fi
archive_existing "$STATE_DIR/iam-policy.created"
archive_existing "$STATE_DIR/iam-policy.create-intent"

# BOTH invoke paths must be reverted. Moving the alias alone leaves an ESM bound to $LATEST.
if [[ "$live" != "$old_sha" ]]; then
  rev="$(live_revision "$FUNCTION")"
  rollback_error="$STATE_DIR/rollback-code.err"
  archive_existing "$rollback_error"
  response_sha="$(aws lambda update-function-code --region "$REGION" \
    --function-name "$FUNCTION" --zip-file "fileb://$STATE_DIR/backup.zip" \
    --revision-id "$rev" --query CodeSha256 --output text 2>"$rollback_error")" || {{
    echo "FATAL: rollback code update failed" >&2
    exit "$(classify_aws_error "$rollback_error")"
  }}
  [[ "$response_sha" == "$old_sha" ]] || {{
    echo "FATAL: rollback response CodeSha256 $response_sha != backup $old_sha" >&2
    exit 43
  }}
  aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION" || {{
    echo "FATAL: timed out waiting for Lambda code rollback" >&2
    exit 42
  }}
  now="$(live_code_sha "$FUNCTION")"
  [[ "$now" == "$old_sha" ]] || {{
    echo "FATAL: restored CodeSha256 $now != backup $old_sha" >&2
    exit 43
  }}
fi
if [[ "$old_alias" != "$(alias_version)" ]]; then
  rollback_alias_error="$STATE_DIR/rollback-alias.err"
  archive_existing "$rollback_alias_error"
  aws lambda update-alias --region "$REGION" --function-name "$FUNCTION" \\
    --name "$ALIAS" --function-version "$old_alias" \\
    --revision-id "$(alias_revision)" >/dev/null 2>"$rollback_alias_error" || {{
      sed -n '1,5p' "$rollback_alias_error" >&2
      echo "FATAL: alias rollback failed" >&2
      exit "$(classify_aws_error "$rollback_alias_error")"
    }}
fi
rollback_invoke_ok '$LATEST' || {{
  rc=$?
  echo "FATAL: restored \\$LATEST did not satisfy the old probe contract" >&2
  exit "$rc"
}}
rollback_invoke_ok "$ALIAS" || {{
  rc=$?
  echo "FATAL: restored alias did not satisfy the old probe contract" >&2
  exit "$rc"
}}
archive_existing "$STATE_DIR/applied.sha256"
archive_existing "$STATE_DIR/alias.version"
archive_existing "$STATE_DIR/complete"
echo "ROLLED_BACK $FUNCTION code=$old_sha alias=$old_alias"
"""


# CDK source that decides WHICH qualifier an event source mapping consumes. If a patch touches
# one of these, the hot-applied binding and the template's binding can disagree, and the next
# `cdk deploy` silently reverts the hot change. That has to surface at GENERATION time — an
# operator finding it mid-rollout has already taken a lease and published an anchor.
ESM_BINDING_SOURCES = (
    "deploy/lib/dispatch_infra.py",
    "deploy/stacks/lambdas.py",
)


def _assert_no_unresolved_esm_conflict(manifest):
    """Refuse to compile when the patch changes the CDK source that owns an ESM binding unless
    the kit states what happens to that binding.

    Why this cannot be deferred: `api_fn.add_event_source_mapping(...)` renders an UNQUALIFIED
    function ARN, i.e. the async consumer runs $LATEST. Hot-repointing it to the alias is a
    real, safe operation (measured: in-place, 11s, config preserved) but it is INVISIBLE to the
    template, so a later `cdk deploy` puts it back. Either the kit declares that it leaves the
    binding alone, or it declares the follow-up needed to make the template agree."""
    touched = sorted(
        path for path in manifest.get("paths", {}) if path in ESM_BINDING_SOURCES
    )
    if not touched:
        return
    decl = (manifest.get("lambda_functions") or [{}])[0].get("esm_binding_conflict")
    if decl not in ("LEAVES_BINDING_UNCHANGED", "REQUIRES_TEMPLATE_FOLLOW_UP"):
        raise SystemExit(
            "this patch changes CDK source that owns an event-source-mapping binding "
            f"({', '.join(touched)}), so the hot state and the template can disagree and the "
            "next cdk deploy would revert a hot repoint. Declare "
            "lambda_functions[0].esm_binding_conflict as either "
            "'LEAVES_BINDING_UNCHANGED' (this kit does not repoint any ESM) or "
            "'REQUIRES_TEMPLATE_FOLLOW_UP' (it does, and the follow-up is recorded in "
            "esm_binding_follow_up)."
        )
    if decl == "REQUIRES_TEMPLATE_FOLLOW_UP":
        follow = (
            manifest["lambda_functions"][0].get("esm_binding_follow_up") or ""
        ).strip()
        if not follow:
            raise SystemExit(
                "esm_binding_conflict=REQUIRES_TEMPLATE_FOLLOW_UP needs "
                "esm_binding_follow_up naming the template change, or the hot repoint is "
                "silently temporary."
            )


def _assert_auto_appliable(manifest, root):
    """Same gate the host compiler enforces. Without it a MANUAL_REVIEW kit — one whose flagged
    operations a human was supposed to handle first — compiles into an executable recipe."""
    status = manifest.get("status")
    if status != "READY":
        raise SystemExit(
            f"preflight: manifest.status={status} (not READY) — a MANUAL_REVIEW/BLOCKED kit is "
            f"not auto-appliable; a human must handle the flagged operations first."
        )
    for path, spec in manifest.get("paths", {}).items():
        if not path.startswith(root.rstrip("/") + "/"):
            continue
        if spec.get("artifact_status") != "SHIPPED":
            continue
        for op in spec.get("operations", []):
            if op.get("class") != "AUTO_CLI":
                raise SystemExit(
                    f"preflight: {path} op class={op.get('class')} is not AUTO_CLI — a compiled "
                    f"recipe only auto-applies AUTO_CLI operations."
                )


def compile_lambda_kit(kit, repo):
    with open(os.path.join(kit, "manifest.json")) as handle:
        manifest = json.load(handle)
    _assert_no_unresolved_esm_conflict(manifest)
    fns = manifest.get("lambda_functions") or []
    if len(fns) != 1:
        raise SystemExit(
            "a compiled Lambda kit declares exactly one entry in `lambda_functions` "
            f"(found {len(fns)}) — one function per kit, so each gets its own verify and "
            "rollback"
        )
    fn = fns[0]
    for key in ("function_name", "package_root"):
        if not fn.get(key):
            raise SystemExit(f"lambda_functions[0].{key} is required")
    _validate_configuration(fn)

    _assert_auto_appliable(manifest, fn["package_root"])

    sources = []
    base_map = {}
    for path, spec in manifest.get("paths", {}).items():
        if not path.startswith(fn["package_root"].rstrip("/") + "/"):
            continue
        # The overlay only ADDS and REPLACES files. A deleted or renamed module would stay in
        # the package after apply, so the patched code would run alongside the module it was
        # supposed to remove. Refuse rather than compile a kit that silently under-delivers.
        change = spec.get("change")
        if change in ("D", "R"):
            raise SystemExit(
                f"{path}: change={change} — the overlay cannot delete or rename a module "
                f"(it only adds and replaces), so this path would be silently skipped. "
                f"Handle it by hand or extend the lane with deletion semantics."
            )
        if spec.get("artifact_status") != "SHIPPED":
            continue
        if os.path.basename(path) == "requirements.txt":
            raise SystemExit(
                f"{path} changed: the overlay reuses the customer's installed deps, so a "
                "dependency change cannot be applied this way. Handle it by hand."
            )
        data = _git_blob(repo, manifest["patch_sha"], path)
        if data is None:
            raise SystemExit(f"cannot read {path} at {manifest['patch_sha']}")
        if _sha(data) != spec.get("patch_sha256"):
            raise SystemExit(f"{path}: blob does not match declared patch_sha256")
        rel = path[len(fn["package_root"].rstrip("/")) + 1 :]
        sources.append((path, rel, data))
        # An already-patched file is NOT drift: a rerun must converge, not refuse. So the live
        # file is acceptable when it equals either the base it was built against or the patch
        # itself. Anything else is someone else's change and must stop the run.
        base_map[rel] = spec.get("base_sha256")
    if not sources:
        raise SystemExit(f"no shipped source under {fn['package_root']}")

    rid = lambda_recipe_id(fn["function_name"])
    common = _common(manifest, fn)
    overlay_payload = {
        "base_hashes": base_map,
        "patch_hashes": {
            rel: hashlib.sha256(data).hexdigest() for _, rel, data in sources
        },
        "sources": {
            rel: base64.b64encode(data).decode() for _, rel, data in sources
        },
    }
    outputs = {
        "apply.sh": _apply(common),
        "verify.sh": _verify(common),
        "rollback.sh": _rollback(common),
        "lambda-state.py": _state_helper(),
        "payload/overlay.json": json.dumps(
            overlay_payload, sort_keys=True, separators=(",", ":")
        )
        + "\n",
        "payload/verify-payload.json": json.dumps(
            fn.get("verify_payload") or {}, sort_keys=True, separators=(",", ":")
        )
        + "\n",
        "payload/verify-expect.json": (
            json.dumps(fn["verify_expect"], sort_keys=True, separators=(",", ":"))
            + "\n"
            if fn.get("verify_expect")
            else ""
        ),
        "payload/rollback-verify-payload.json": json.dumps(
            fn.get("rollback_verify_payload")
            or fn.get("verify_payload")
            or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        "payload/rollback-verify-expect.json": (
            json.dumps(
                fn["rollback_verify_expect"],
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            if fn.get("rollback_verify_expect")
            else ""
        ),
        "payload/lambda-config.json": json.dumps(
            {
                "environment_updates": fn.get("environment_updates") or {},
                "generated_environment": fn.get("generated_environment") or {},
                "iam_read_tables": fn.get("iam_read_tables") or [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    }
    written = []
    for name, content in outputs.items():
        rel = f"lib/compiled/{rid}/{name}"
        dest = os.path.join(kit, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as handle:
            handle.write(content)
        if name.endswith(".sh"):
            os.chmod(dest, 0o755)
        manifest.setdefault("kit_files", {})[rel] = {"sha256": _sha(content.encode())}
        written.append(rel)

    with open(os.path.join(kit, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return {
        "resource_id": rid,
        "function": fn["function_name"],
        "alias": fn.get("alias") or "live",
        "source_count": len(sources),
        "files": written,
    }


def main(argv):
    if len(argv) != 3:
        print("usage: _compile_lambda.py <patch-kit> <source-repo>", file=sys.stderr)
        return 2
    print(
        json.dumps(
            compile_lambda_kit(os.path.abspath(argv[1]), os.path.abspath(argv[2])),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
