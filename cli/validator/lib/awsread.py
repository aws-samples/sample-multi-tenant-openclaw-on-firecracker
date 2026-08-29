import shlex
import urllib.error
import urllib.request

try:
    import boto3
except ImportError:
    boto3 = None
try:
    from botocore.exceptions import BotoCoreError, ClientError
    AWS_ERRORS = (BotoCoreError, ClientError)
except ImportError:
    AWS_ERRORS = ()


BOTO3_AVAILABLE = boto3 is not None
ALLOWED_PREFIXES = ("get", "list", "describe")
ALLOWED_EXACT = {
    "simulate_principal_policy",
    "filter_log_events",
    "head_object",
    "get_object",
    "send_command",
}
READ_ONLY_ALLOWLIST = set(ALLOWED_EXACT).union({
    "describe_auto_scaling_groups", "describe_instance_information",
    "describe_instances", "describe_stacks",
    "get_export", "get_function", "get_function_configuration",
    "get_bucket_versioning", "get_command_invocation",
    "get_metric_statistics", "get_parameter", "get_parameters_by_path",
    "get_stages",
    "get_queue_attributes", "list_aliases", "list_event_source_mappings",
    # Prefix-based validation already permits this; keep it explicit for the
    # validator-wide method inventory asserted by the unit tests.
    "list_functions", "list_object_versions", "list_objects_v2",
    "list_stack_resources", "describe_alarms",
    "describe_launch_templates", "describe_launch_template_versions",
    "describe_table",
})
SSM_COMMAND_PREFIXES = (
    "sha256sum ",
    "stat ",
    "iptables -S",
    "cat ",
    "systemctl is-active ",
    "journalctl ",
)
_DEFAULT_FACTORY = object()


class ReadOnlyViolation(RuntimeError):
    pass


class AwsUnavailable(RuntimeError):
    pass


class AwsReadError(RuntimeError):
    pass


def assert_read_only_method(method):
    name = str(method)
    allowed = name.startswith(ALLOWED_PREFIXES) or name in ALLOWED_EXACT
    if not allowed:
        raise ReadOnlyViolation("AWS method is not read-only: %s" % name)
    return name


def _ssm_commands(kwargs):
    params = kwargs.get("Parameters") or kwargs.get("parameters") or {}
    commands = params.get("commands") or params.get("Commands") or []
    return [commands] if isinstance(commands, str) else list(commands)


def assert_read_only_payload(service, method, kwargs):
    if service != "ssm" or method != "send_command":
        return
    commands = _ssm_commands(kwargs)
    if not commands:
        raise ReadOnlyViolation("send_command requires a non-empty commands payload")
    for command in commands:
        text = str(command).strip()
        if not text.startswith(SSM_COMMAND_PREFIXES):
            raise ReadOnlyViolation("SSM command is not permitted: %s" % text)
        if any(token in text for token in (";", "&&", "||", "\n", "\r")):
            raise ReadOnlyViolation("SSM command chaining is prohibited")


class AwsReader:
    def __init__(self, region=None, client_factory=_DEFAULT_FACTORY):
        self.region = region
        if client_factory is not _DEFAULT_FACTORY:
            self._client_factory = client_factory
        elif boto3 is not None:
            self._client_factory = boto3.client
        else:
            self._client_factory = None
        self._clients = {}

    @property
    def available(self):
        return self._client_factory is not None

    def _client(self, service):
        if not self.available:
            raise AwsUnavailable("boto3 is not installed")
        if service not in self._clients:
            kwargs = {"region_name": self.region} if self.region else {}
            self._clients[service] = self._client_factory(service, **kwargs)
        return self._clients[service]

    def call(self, service, method, **kwargs):
        name = assert_read_only_method(method)
        assert_read_only_payload(service, name, kwargs)
        try:
            return getattr(self._client(service), name)(**kwargs)
        except (AttributeError, TypeError, ValueError, OSError) as error:
            raise AwsReadError("%s.%s: %s" % (service, name, error))
        except AWS_ERRORS as error:
            raise AwsReadError("%s.%s: %s" % (service, name, error))

    def body_bytes(self, response, key="Body"):
        body = response.get(key)
        if body is None:
            return b""
        return body.read() if hasattr(body, "read") else bytes(body)

    def lambda_package(self, function_name, qualifier=None):
        kwargs = {"FunctionName": function_name}
        if qualifier:
            kwargs["Qualifier"] = qualifier
        response = self.call("lambda", "get_function", **kwargs)
        direct = response.get("CodeBytes")
        if direct is not None:
            return bytes(direct)
        location = (response.get("Code") or {}).get("Location")
        if not location:
            raise AwsReadError("get_function returned no package location")
        try:
            with urllib.request.urlopen(location) as handle:
                return handle.read()
        except (urllib.error.URLError, ValueError, OSError) as error:
            raise AwsReadError("package download failed: %s" % error)


def quote_path(path):
    return shlex.quote(str(path))
