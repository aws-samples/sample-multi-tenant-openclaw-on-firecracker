from lib.awsread import AwsReadError, AwsUnavailable


DEFAULT_STACK_NAMES = (
    "OpenClawOrchestrator",
    "OpenClawImage",
    "OpenClawHostImage",
)

COORDINATES = (
    "assets_bucket",
    "buckets",
    "host_asg",
    "edge_asg",
    "host_lt",
    "edge_lt",
    "lambda_functions",
    "lambda_logical_to_physical",
    "rest_api_id",
    "tables",
    "redis_group",
)


def _physical(summary):
    return str(summary.get("PhysicalResourceId") or "").strip()


def _logical(summary):
    return str(summary.get("LogicalResourceId") or "").strip()


def _role_hint(value):
    text = str(value or "").lower()
    roles = [
        role for role in ("edge", "host")
        if role in text
    ]
    return roles[0] if len(roles) == 1 else None


def _resource_role(logical, physical):
    logical_role = _role_hint(logical)
    if logical_role:
        return logical_role, "logical_id"
    physical_role = _role_hint(physical)
    if physical_role:
        return physical_role, "physical_id"
    return None, None


def _list_resources(reader, stack_name):
    rows = []
    token = None
    while True:
        kwargs = {"StackName": stack_name}
        if token:
            kwargs["NextToken"] = token
        response = reader.call(
            "cloudformation", "list_stack_resources", **kwargs)
        rows.extend(response.get("StackResourceSummaries") or [])
        token = response.get("NextToken")
        if not token:
            return rows


def _reason(name, found_stacks):
    if not found_stacks:
        return "no candidate CloudFormation stack could be described"
    return "no matching CloudFormation resource was declared in %s" % (
        ", ".join(found_stacks))


def discover(reader, region, stack_names=None):
    del region  # Region belongs to the reader's client construction.
    candidates = list(stack_names or DEFAULT_STACK_NAMES)
    stacks = []
    resources = []
    stack_errors = {}

    for candidate in candidates:
        try:
            response = reader.call(
                "cloudformation", "describe_stacks", StackName=candidate)
        except (AwsReadError, AwsUnavailable) as error:
            stack_errors[candidate] = str(error)
            continue
        described = response.get("Stacks") or []
        if not described:
            stack_errors[candidate] = "describe_stacks returned no stack"
            continue
        stack = described[0]
        name = str(stack.get("StackName") or candidate)
        try:
            summaries = _list_resources(reader, name)
        except (AwsReadError, AwsUnavailable) as error:
            stack_errors[name] = str(error)
            summaries = []
        stacks.append({
            "name": name,
            "outputs": {
                str(item.get("OutputKey")): item.get("OutputValue")
                for item in stack.get("Outputs") or []
                if item.get("OutputKey")
            },
            "parameters": {
                str(item.get("ParameterKey")): item.get("ParameterValue")
                for item in stack.get("Parameters") or []
                if item.get("ParameterKey")
            },
        })
        for summary in summaries:
            item = dict(summary)
            item["_stack_name"] = name
            resources.append(item)

    buckets = []
    host_asg = None
    edge_asg = None
    host_lt = None
    edge_lt = None
    lambda_functions = []
    lambda_map = {}
    rest_api_id = None
    tables = []
    redis_group = None
    classifications = {}

    for resource in resources:
        resource_type = resource.get("ResourceType")
        physical = _physical(resource)
        logical = _logical(resource)
        if not physical:
            continue
        if resource_type == "AWS::S3::Bucket":
            buckets.append(physical)
        elif resource_type == "AWS::AutoScaling::AutoScalingGroup":
            role, classified_by = _resource_role(logical, physical)
            if role == "edge" and edge_asg is None:
                edge_asg = edge_asg or physical
                classifications["edge_asg"] = {
                    "classified_by": classified_by,
                    "logical_id": logical,
                    "physical_id": physical,
                }
            elif role == "host" and host_asg is None:
                host_asg = host_asg or physical
                classifications["host_asg"] = {
                    "classified_by": classified_by,
                    "logical_id": logical,
                    "physical_id": physical,
                }
        elif resource_type == "AWS::EC2::LaunchTemplate":
            role, classified_by = _resource_role(logical, physical)
            if role == "edge" and edge_lt is None:
                edge_lt = edge_lt or physical
                classifications["edge_lt"] = {
                    "classified_by": classified_by,
                    "logical_id": logical,
                    "physical_id": physical,
                }
            elif role == "host" and host_lt is None:
                host_lt = host_lt or physical
                classifications["host_lt"] = {
                    "classified_by": classified_by,
                    "logical_id": logical,
                    "physical_id": physical,
                }
        elif resource_type == "AWS::Lambda::Function":
            lambda_functions.append(physical)
            if logical:
                lambda_map[logical] = physical
        elif resource_type == "AWS::ApiGateway::RestApi":
            rest_api_id = rest_api_id or physical
        elif resource_type == "AWS::DynamoDB::Table":
            tables.append(physical)
        elif resource_type == "AWS::ElastiCache::ReplicationGroup":
            redis_group = redis_group or physical

    found_stack_names = [item["name"] for item in stacks]
    result = {
        "candidate_stacks": candidates,
        "stacks": stacks,
        "stack_errors": stack_errors,
        "resources": resources,
        "resource_types": sorted({
            str(item.get("ResourceType")) for item in resources
            if item.get("ResourceType")
        }),
        "assets_bucket": next(
            (name for name in buckets if "assets" in name.lower()), None),
        "buckets": sorted(set(buckets)),
        "host_asg": host_asg,
        "edge_asg": edge_asg,
        "host_lt": host_lt,
        "edge_lt": edge_lt,
        "lambda_functions": sorted(set(lambda_functions)),
        "lambda_logical_to_physical": lambda_map,
        "rest_api_id": rest_api_id,
        "tables": sorted(set(tables)),
        "redis_group": redis_group,
        "logging_enabled": any(
            item.get("ResourceType") in {
                "AWS::KinesisFirehose::DeliveryStream",
                "AWS::OpenSearchService::Domain",
            }
            for item in resources
        ) if stacks else None,
        "classifications": classifications,
        "unresolved": [],
        "sources": {},
    }

    for name in COORDINATES:
        value = result.get(name)
        resolved = bool(value)
        if resolved:
            result["sources"][name] = "discovered"
        else:
            result["unresolved"].append({
                "coordinate": name,
                "reason": _reason(name, found_stack_names),
            })
    if result["logging_enabled"] is not None:
        result["sources"]["logging_enabled"] = "discovered"
    else:
        result["unresolved"].append({
            "coordinate": "logging_enabled",
            "reason": _reason("logging_enabled", found_stack_names),
        })
    return result
