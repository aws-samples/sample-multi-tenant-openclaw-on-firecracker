import ast
import io
import json
import re
import zipfile

from checks.channels import (
    _body_bytes,
    _coordinate,
    _gateway_bytes,
    _missing,
    _s3_digest,
    _sha,
    core_object_inventory,
)
from lib.awsread import AwsReadError, AwsUnavailable
from lib.result import finding


# CDK emits provider/custom-resource Lambdas under several logical-id shapes, all of which
# have no source under deploy/lambda/. Observed live in a gateway-HEAD deployment:
#   AWS679f53fac002430cb0da5b7982bd22872D164C4C        (singleton runtime)
#   CustomCDKBucketDeployment8693BB6496...             (BucketDeployment)
#   CustomS3AutoDeleteObjectsCustomResourceProvider... (auto-delete)
#   CustomVpcRestrictDefaultSGCustomResourceProvi...   (default-SG restriction)
#   GoldenBuildProviderframeworkonEventBBA66454        (Provider framework)
CDK_PROVIDER_LOGICAL_ID = re.compile(
    r"^AWS[0-9a-f]{32}[A-F0-9]{8}$"
    r"|^Custom[A-Za-z0-9]*(CustomResourceProviderHandler|BucketDeployment)[A-Za-z0-9]*$"
    r"|^[A-Za-z0-9]*Providerframeworkon(Event|IsComplete|Timeout)[A-Za-z0-9]*$")

# A CDK InlineCode function's package is exactly one file. Anything else is a real package.
INLINE_MEMBER_NAMES = ("index.py", "index.mjs", "index.js")


def _gateway_inline_sources(ctx):
    """Every string passed to `_lambda.Code.from_inline(...)` anywhere in deploy/**.

    Returned as {sha256: "path:lineno"} so a deployed index.py can be matched back to the
    exact literal it came from. Module-level string constants are resolved too, because the
    handlers are written as a named triple-quoted constant and passed by name rather than
    inlined at the call site (see deploy/stacks/_helpers.py, which passes
    _TRACK_DEFAULT_HANDLER).
    """
    sources = {}
    try:
        paths = [p for p in ctx.git_tree_paths(ctx.gateway_ref, "deploy")
                 if p.endswith(".py")]
    except AttributeError:
        paths = [str(p.relative_to(ctx.repo))
                 for p in (ctx.repo / "deploy").rglob("*.py")]
    for path in paths:
        try:
            module_text = _gateway_bytes(ctx, path).decode("utf-8", "replace")
            tree = ast.parse(module_text)
        except (OSError, RuntimeError, SyntaxError, ValueError):
            continue
        constants = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for goal in node.targets:
                    if isinstance(goal, ast.Name):
                        constants[goal.id] = (node.value.value, node.lineno)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            attribute = node.func
            if not (isinstance(attribute, ast.Attribute)
                    and attribute.attr == "from_inline"):
                continue
            for argument in node.args:
                literal = None
                lineno = node.lineno
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    literal = argument.value
                elif isinstance(argument, ast.Name) and argument.id in constants:
                    literal, lineno = constants[argument.id]
                if literal is None:
                    continue
                sources[_sha(literal.encode("utf-8"))] = "%s:%d" % (path, lineno)
    return sources


def _lambda_declarations(ctx):
    mapping, source, reason = _coordinate(
        ctx, "lambda_logical_to_physical")
    if not mapping:
        return None, source, reason
    return [
        {"logical_id": logical_id, "function_name": function_name}
        for logical_id, function_name in sorted(mapping.items())
    ], source, None


def check_g1(ctx):
    declarations, source, reason = _lambda_declarations(ctx)
    if not declarations:
        return _missing(
            "G1", "declared Lambda functions", reason,
            {"lambda_source": source})
    rows = []
    failures = []
    missing = []
    for declaration in declarations:
        logical_id = declaration["logical_id"]
        try:
            response = ctx.aws.call(
                "lambda", "get_function_configuration",
                FunctionName=declaration["function_name"])
            rows.append({
                "logical_id": logical_id,
                "exists": True,
                "runtime": response.get("Runtime"),
            })
        except (AwsReadError, AwsUnavailable) as error:
            not_found = (
                "ResourceNotFoundException" in str(error)
                or "not found" in str(error).lower())
            rows.append({
                "logical_id": logical_id,
                "exists": False,
                "read_error": type(error).__name__,
            })
            if not_found:
                failures.append(logical_id)
            else:
                missing.append(logical_id)
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "G1", verdict,
        "CloudFormation-declared Lambda function resolution was checked directly.",
        {"inspected": len(rows), "lambda_source": source,
         "functions": rows, "unreadable": missing},
        failures,
        "Restore deleted declared functions or repair their CloudFormation stack.",
    )


def _gateway_lambda_candidates(ctx):
    try:
        paths = ctx.git_tree_paths(ctx.gateway_ref, "deploy/lambda")
    except AttributeError:
        root = ctx.repo / "deploy" / "lambda"
        paths = [
            str(path.relative_to(ctx.repo))
            for path in root.rglob("*") if path.is_file()
        ]
    candidates = {}
    for path in paths:
        match = re.match(r"deploy/lambda/([^/]+)/(.+)$", path)
        if not match:
            continue
        candidate, relative = match.groups()
        candidates.setdefault(candidate, {})[relative] = path
    return candidates


def _normal_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _select_lambda_candidate(logical_id, names, candidates):
    logical = _normal_name(logical_id)
    best = None
    best_score = -1
    package_names = set(names)
    for candidate, files in candidates.items():
        overlap = len(package_names.intersection(files))
        name_score = 1000 if _normal_name(candidate) in logical else 0
        score = name_score + overlap
        if overlap and score > best_score:
            best = candidate
            best_score = score
    return best


def _vendored_allowlist(archive):
    allowed = set()
    names = archive.namelist()
    for name in names:
        if ".dist-info/" not in name:
            continue
        if name.endswith("/top_level.txt"):
            text = archive.read(name).decode("utf-8", "replace")
            allowed.update(
                line.strip().split(".", 1)[0]
                for line in text.splitlines() if line.strip())
        elif name.endswith("/RECORD"):
            text = archive.read(name).decode("utf-8", "replace")
            for line in text.splitlines():
                path = line.split(",", 1)[0].strip()
                if path and "/" in path:
                    allowed.add(path.split("/", 1)[0])
        top = name.split("/", 1)[0]
        if top.endswith(".dist-info") or top.endswith(".data"):
            allowed.add(top)
    return allowed


def _is_bytecode_member(name):
    parts = str(name).split("/")
    return "__pycache__" in parts or str(name).endswith(".pyc")


def _is_cdk_provider(logical_id):
    return bool(CDK_PROVIDER_LOGICAL_ID.fullmatch(str(logical_id or "")))


def _lambda_package_comparison(ctx, logical_id, package, candidates):
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        archive_names = sorted(
            name for name in archive.namelist()
            if name and not name.endswith("/"))
        ignored_bytecode = [
            name for name in archive_names
            if _is_bytecode_member(name)
        ]
        names = [
            name for name in archive_names
            if not _is_bytecode_member(name)
        ]
        candidate = _select_lambda_candidate(logical_id, names, candidates)
        if not candidate:
            # A CDK InlineCode function has exactly one member and no package directory.
            # Its source is a string literal in deploy/stacks/**, so it is still verifiable
            # -- which matters, because otherwise "modify an inline Lambda" would be
            # undetectable and the check would sit at UNVERIFIED forever.
            if len(names) == 1 and names[0] in INLINE_MEMBER_NAMES:
                deployed = archive.read(names[0])
                digest = _sha(deployed)
                inline = _gateway_inline_sources(ctx)
                origin = inline.get(digest)
                return {
                    "candidate": None,
                    "classification": "inline_source",
                    "inline_member": names[0],
                    "inline_origin": origin,
                    "members": [{
                        "member": names[0],
                        "gateway_path": origin,
                        "deployed_sha256": digest,
                        "gateway_sha256": digest if origin else None,
                        "match": bool(origin),
                    }],
                    "member_count": 1,
                    "archive_member_count": len(archive_names),
                    "vendored_allowlist": [],
                    "accepted_extras": [],
                    "extra": [],
                    "ignored_bytecode": ignored_bytecode,
                    "failures": [] if origin else [names[0]],
                }, None
            return None, "no gateway Lambda directory matched the package"
        gateway = candidates[candidate]
        vendored = _vendored_allowlist(archive)
        rows = []
        failures = []
        extras = []
        for name in names:
            deployed_sha = _sha(archive.read(name))
            source_path = gateway.get(name)
            if source_path:
                gateway_sha = _sha(_gateway_bytes(ctx, source_path))
                match = deployed_sha == gateway_sha
                if not match:
                    failures.append(name)
                rows.append({
                    "member": name,
                    "gateway_path": source_path,
                    "deployed_sha256": deployed_sha,
                    "gateway_sha256": gateway_sha,
                    "match": match,
                })
                continue
            extras.append(name)
        return {
            "candidate": candidate,
            "members": rows,
            "member_count": len(names),
            "archive_member_count": len(archive_names),
            "vendored_allowlist": sorted(vendored),
            "accepted_extras": extras,
            "extra": extras,
            "ignored_bytecode": ignored_bytecode,
            "failures": failures,
        }, None


def _code_sha(response):
    return str((response.get("Configuration") or {}).get(
        "CodeSha256") or response.get("CodeSha256") or "")


def check_g2(ctx):
    declarations, source, reason = _lambda_declarations(ctx)
    if not declarations:
        return _missing(
            "G2", "Lambda code packages", reason,
            {"lambda_source": source})
    try:
        candidates = _gateway_lambda_candidates(ctx)
    except (OSError, RuntimeError) as error:
        return _missing("G2", "Lambda code packages", error)
    rows = []
    failures = []
    missing = []
    unverified = []
    not_gateway_package = []
    matched_directories = set()
    for declaration in declarations:
        logical_id = declaration["logical_id"]
        function_name = declaration["function_name"]
        if _is_cdk_provider(logical_id):
            rows.append({
                "logical_id": logical_id,
                "classification": "cdk_provider",
                "source_directory": None,
                "package": None,
                "package_error": None,
                "aliases": [],
                "neutral": True,
            })
            continue
        try:
            latest_response = ctx.aws.call(
                "lambda", "get_function",
                FunctionName=function_name, Qualifier="$LATEST")
            package = ctx.aws.lambda_package(function_name, "$LATEST")
            package_row, package_error = _lambda_package_comparison(
                ctx, logical_id, package, candidates)
            aliases_response = ctx.aws.call(
                "lambda", "list_aliases", FunctionName=function_name)
            alias_rows = []
            aliases = aliases_response.get("Aliases") or []
            for alias in aliases:
                alias_name = str(alias.get("Name") or "")
                alias_response = ctx.aws.call(
                    "lambda", "get_function",
                    FunctionName=function_name, Qualifier=alias_name)
                latest_sha = _code_sha(latest_response)
                alias_sha = _code_sha(alias_response)
                if not latest_sha or not alias_sha:
                    same = None
                    missing.append("%s:%s:code_sha256" % (
                        logical_id, alias_name))
                else:
                    same = latest_sha == alias_sha
                if same is False:
                    failures.append("%s:%s" % (logical_id, alias_name))
                alias_rows.append({
                    "alias": alias_name,
                    "version": str(alias.get("FunctionVersion") or ""),
                    "latest_code_sha256": latest_sha,
                    "alias_code_sha256": alias_sha,
                    "match": same,
                })
            # Having no alias is the normal shape for most functions in this stack -- only
            # the API handler is fronted by a `live` alias. Recording every alias-less
            # function as missing evidence pushed G2 to UNVERIFIED on a healthy control
            # plane and buried the one comparison that matters. An unqualified function is
            # served from $LATEST, so there is nothing to reconcile.
            pass
            if package_error:
                # Still UNVERIFIED on purpose. The two explainable shapes are handled
                # before this point -- CDK provider/custom-resource logical ids by
                # _is_cdk_provider, and InlineCode functions by comparing their single
                # index.py against the string literals in deploy/stacks/**. Anything left
                # is a deployed function whose source this tool genuinely cannot locate,
                # and saying so is the honest answer.
                unverified.append("%s:package" % logical_id)
                not_gateway_package.append(logical_id)
            elif package_row:
                if package_row.get("candidate"):
                    matched_directories.add(package_row["candidate"])
                failures.extend(
                    "%s:%s" % (logical_id, member)
                    for member in package_row["failures"])
            rows.append({
                "logical_id": logical_id,
                "classification": (
                    "unmatched" if package_error else "gateway_source"),
                "source_directory": (
                    package_row["candidate"] if package_row else None),
                "package": package_row,
                "package_error": package_error,
                "aliases": alias_rows,
            })
        except (AwsReadError, AwsUnavailable, OSError,
                ValueError, zipfile.BadZipFile) as error:
            missing.append(logical_id)
            rows.append({
                "logical_id": logical_id,
                "package_error": "%s: %s" % (type(error).__name__, error),
                "aliases": [],
            })
    # Informational, deliberately NOT a failure: several deploy/lambda/* packages are
    # feature-gated by config (agentcore_tools, audit_archive, platform_authorizer,
    # pretokengen, ptg_attach, tenant_stats), so "declared in the tree but not deployed" is
    # a configuration choice rather than drift. What protects the neutral classification
    # above from hiding a vanished function is G1, which asserts every CloudFormation-
    # declared function resolves -- CloudFormation is the declaration of what should exist.
    undeployed_directories = sorted(set(candidates) - matched_directories)
    verdict = (
        "FAIL" if failures
        else ("UNVERIFIED" if unverified
              else ("INCONCLUSIVE" if missing else "PASS")))
    return finding(
        "G2", verdict,
        "Lambda package members and alias-served code hashes were compared.",
        {"inspected": len(rows), "lambda_source": source,
         "functions": rows, "missing": missing,
         "unverified": unverified,
         "not_gateway_package": sorted(not_gateway_package),
         "gateway_packages_declared": sorted(candidates),
         "gateway_packages_matched": sorted(matched_directories),
         "gateway_packages_not_deployed": undeployed_directories},
        sorted(set(failures)),
        "Redeploy changed Lambda members and move aliases only after code hashes converge.",
    )


def _literal_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resource_parent(call, resources):
    owner = call.func.value
    if isinstance(owner, ast.Name):
        return resources.get(owner.id)
    if (isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "api" and owner.attr == "root"):
        return ""
    return None


def _source_routes(ctx):
    try:
        paths = ctx.git_tree_paths(ctx.gateway_ref, "deploy/stacks")
    except AttributeError:
        root = ctx.repo / "deploy" / "stacks"
        paths = [
            str(path.relative_to(ctx.repo))
            for path in root.rglob("*.py") if path.is_file()
        ]
    routes = set()
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(
                _gateway_bytes(ctx, path).decode("utf-8", "replace"))
        except SyntaxError:
            continue
        resources = {}
        nodes = sorted(ast.walk(tree), key=lambda node: getattr(node, "lineno", 0))
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if not isinstance(node, ast.Assign) or not isinstance(
                        node.value, ast.Call):
                    continue
                call = node.value
                if not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr != "add_resource" or not call.args:
                    continue
                segment = _literal_string(call.args[0])
                parent = _resource_parent(call, resources)
                if segment is None or parent is None:
                    continue
                value = (parent.rstrip("/") + "/" + segment).replace("//", "/")
                for target in node.targets:
                    if isinstance(target, ast.Name) and resources.get(target.id) != value:
                        resources[target.id] = value
                        changed = True
        for node in nodes:
            if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute):
                continue
            if node.func.attr != "add_method" or not node.args:
                continue
            method = _literal_string(node.args[0])
            owner = node.func.value
            path_value = resources.get(owner.id) if isinstance(owner, ast.Name) else None
            if method and path_value is not None:
                routes.add((path_value or "/", method.upper()))
    return sorted(routes)


def _export_document(ctx, response):
    body = response.get("body") if "body" in response else response.get("Body")
    raw = body.read() if hasattr(body, "read") else body
    return json.loads(bytes(raw or b"{}").decode("utf-8"))


def _stage_name(ctx, api_id):
    supplied = ctx.get("control_plane_api.deployed_stages", []) or []
    if supplied:
        first = supplied[0]
        return first.get("stage") or first.get("stageName"), "environment-json"
    response = ctx.aws.call("apigateway", "get_stages", restApiId=api_id)
    rows = response.get("item") or response.get("Items") or []
    if not rows:
        return None, "discovered"
    return rows[0].get("stageName"), "discovered"


def _security_definitions(document):
    definitions = (
        document.get("components", {}).get("securitySchemes", {})
        or document.get("securityDefinitions", {})
        or {})
    api_keys = set()
    authorizers = set()
    for name, value in definitions.items():
        if value.get("type") == "apiKey" and value.get("name") == "x-api-key":
            api_keys.add(name)
        if value.get("x-amazon-apigateway-authorizer"):
            authorizers.add(name)
    return api_keys, authorizers


def _operation_shape(operation, api_keys, authorizers):
    security = operation.get("security") or []
    names = {
        name for item in security if isinstance(item, dict)
        for name in item
    }
    explicit_key = operation.get("apiKeyRequired")
    if explicit_key is None:
        explicit_key = operation.get("x-amazon-apigateway-api-key-required")
    api_key_required = (
        bool(explicit_key) if explicit_key is not None
        else bool(names.intersection(api_keys)))
    explicit_auth = (
        operation.get("authorizationType")
        or (operation.get("x-amazon-apigateway-auth") or {}).get("type"))
    if explicit_auth:
        authorization = str(explicit_auth).upper()
    elif names.intersection(authorizers):
        authorization = "CUSTOM"
    else:
        authorization = "NONE"
    return {
        "api_key_required": api_key_required,
        "authorization": authorization,
    }


def check_g3(ctx):
    api_id, api_source, api_error = _coordinate(ctx, "rest_api_id")
    if not api_id:
        return _missing(
            "G3", "API Gateway routes", api_error,
            {"rest_api_source": api_source})
    try:
        expected = _source_routes(ctx)
        stage, stage_source = _stage_name(ctx, api_id)
        if not stage:
            return _missing(
                "G3", "API Gateway routes", "no deployed stage was found",
                {"rest_api_source": api_source,
                 "stage_source": stage_source})
        response = ctx.aws.call(
            "apigateway", "get_export",
            restApiId=api_id, stageName=stage, exportType="oas30",
            parameters={"extensions": "integrations"})
        document = _export_document(ctx, response)
    except (AwsReadError, AwsUnavailable, OSError, RuntimeError,
            ValueError, TypeError) as error:
        return _missing("G3", "API Gateway routes", error)
    if not expected:
        return _missing("G3", "API Gateway routes",
                        "no source add_method routes were parsed")
    lambda_map, _map_source, _map_error = _coordinate(
        ctx, "lambda_logical_to_physical")
    platform_authorizer = any(
        "platformauthorizer" in _normal_name(logical)
        for logical in (lambda_map or {}))
    expected_auth = "CUSTOM" if platform_authorizer else "NONE"
    api_keys, authorizers = _security_definitions(document)
    paths = document.get("paths") or {}
    rows = []
    failures = []
    for path, method in expected:
        operation = (paths.get(path) or {}).get(method.lower())
        if not isinstance(operation, dict):
            failures.append("%s %s" % (method, path))
            rows.append({
                "path": path, "method": method, "present": False,
                "api_key_required": None, "authorization": None,
            })
            continue
        shape = _operation_shape(operation, api_keys, authorizers)
        if not shape["api_key_required"] or shape["authorization"] != expected_auth:
            failures.append("%s %s" % (method, path))
        rows.append({
            "path": path, "method": method, "present": True,
            "api_key_required": shape["api_key_required"],
            "authorization": shape["authorization"],
            "expected_api_key_required": True,
            "expected_authorization": expected_auth,
        })
    return finding(
        "G3", "FAIL" if failures else "PASS",
        "Source-declared API routes and authorization shapes were compared with the stage export.",
        {"inspected": len(rows), "rest_api_source": api_source,
         "stage": stage, "stage_source": stage_source,
         "routes": rows},
        failures,
        "Redeploy missing routes and restore their API-key and authorization configuration.",
    )


def check_g4(ctx):
    bucket, source, reason = _coordinate(ctx, "assets_bucket")
    if not bucket:
        return _missing(
            "G4", "core S3 objects", reason,
            {"assets_bucket_source": source})
    inventory, gaps = core_object_inventory(ctx)
    if not inventory:
        return _missing(
            "G4", "core S3 objects", "no core object inventory was parsed",
            {"inventory_gaps": gaps, "assets_bucket_source": source})
    versioning = "unknown"
    try:
        response = ctx.aws.call(
            "s3", "get_bucket_versioning", Bucket=bucket)
        versioning = str(response.get("Status") or "disabled").lower()
    except (AwsReadError, AwsUnavailable):
        versioning = "unreadable"
    rows = []
    failures = []
    missing = list(gaps)
    for item in inventory:
        key = item["key"]
        try:
            observed = _s3_digest(ctx, bucket, key)
            match = observed["sha256"] == item["expected_sha256"]
            if not match:
                failures.append(key)
            rows.append({
                "channel": item["channel"], "key": key,
                "expected_sha256": item["expected_sha256"],
                "s3_sha256": observed["sha256"],
                "match": match,
                "version_id": (
                    observed.get("version_id")
                    if versioning == "enabled" else None),
            })
        except (AwsReadError, AwsUnavailable):
            missing.append(key)
            rows.append({
                "channel": item["channel"], "key": key,
                "expected_sha256": item["expected_sha256"],
                "s3_sha256": None, "match": None,
                "version_id": None,
            })
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "G4", verdict,
        "Core channel objects were checked for presence, digest, and current version identity.",
        {"inspected": len(rows), "assets_bucket_source": source,
         "bucket_versioning": versioning,
         "versioning_note": (
             "current VersionIds recorded" if versioning == "enabled"
             else "bucket versioning is absent or unreadable; this is not a failure"),
         "objects": rows, "inventory_gaps": missing},
        sorted(set(failures)),
        "Restore deleted or replaced core objects from the matching gateway bytes.",
    )
