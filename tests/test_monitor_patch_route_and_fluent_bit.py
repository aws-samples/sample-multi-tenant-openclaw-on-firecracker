import ast
import hashlib
import importlib.util
import json
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, check_output
import sys
from decimal import Decimal
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_route_ops():
    path = ROOT / "deploy" / "userdata" / "route_ops.py"
    spec = importlib.util.spec_from_file_location("monitor_patch_route_ops", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_function(path, function_name):
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[function_name]


def _load_tenant_stats_service(monkeypatch, table):
    core = ModuleType("core")
    core.__path__ = []
    auth = ModuleType("core.auth")
    auth._get_caller_identity = lambda event: {
        "is_admin": True,
        "platform_scope": None,
    }
    clients = ModuleType("core.clients")
    clients.tenant_stats_table = table
    utils = ModuleType("core.utils")
    utils._err = lambda code, error_code, message: {
        "statusCode": code,
        "body": json.dumps({"code": error_code, "error": message}),
    }
    utils._resp = lambda code, body: {
        "statusCode": code,
        "body": json.dumps(body),
    }
    for name, module in {
        "core": core,
        "core.auth": auth,
        "core.clients": clients,
        "core.utils": utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = (
        ROOT
        / "deploy"
        / "lambda"
        / "api"
        / "services"
        / "tenant_stats_service.py"
    )
    spec = importlib.util.spec_from_file_location("monitor_patch_tenant_stats", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bitmap_rebuild_replaces_cross_process_state(monkeypatch):
    route_ops = _load_route_ops()
    bitmap = route_ops.PortBitmap(low=10000, high=10002)
    bitmap.mark_used(10000)
    bitmap.mark_used(10001)
    monkeypatch.setattr(route_ops, "list_dnat_rules", lambda: {10002: "10.0.0.2"})

    assert route_ops.rebuild_bitmap_from_iptables(bitmap) == 1
    assert bitmap.snapshot() == {10002}


def test_delete_route_releases_live_and_legacy_rules(monkeypatch):
    route_ops = _load_route_ops()
    calls = []

    monkeypatch.setattr(route_ops, "rebuild_bitmap_from_iptables", lambda bitmap: 1)
    monkeypatch.setattr(
        route_ops,
        "release_port_and_dnat",
        lambda bitmap, port, guest: calls.append(("live", port, guest)),
    )
    monkeypatch.setattr(
        route_ops,
        "dnat_remove_all",
        lambda port, guest: calls.append(("legacy", port, guest)),
    )

    class Writer:
        def del_route(self, tenant_id):
            calls.append(("redis", tenant_id))
            return True

    monkeypatch.setattr(route_ops, "_cli_redis_writer", lambda: Writer())
    rc = route_ops._cli_delete_route(
        "tenant-1", 10001, "10.0.0.2", route_ops.GATEWAY_GUEST_PORT
    )

    assert rc == 0
    assert ("live", 10001, "10.0.0.2") in calls
    assert ("legacy", route_ops.GATEWAY_GUEST_PORT, "10.0.0.2") in calls
    assert ("redis", "tenant-1") in calls


def test_delete_route_fails_when_redis_writer_is_unconfigured(monkeypatch):
    route_ops = _load_route_ops()
    monkeypatch.setattr(route_ops, "rebuild_bitmap_from_iptables", lambda bitmap: 0)
    monkeypatch.setattr(route_ops, "release_port_and_dnat", lambda *args: None)
    monkeypatch.setattr(route_ops, "_cli_redis_writer", lambda: None)

    assert route_ops._cli_delete_route("tenant-1", 10001, "10.0.0.2", 0) == 1


def test_delete_route_fails_when_redis_delete_fails(monkeypatch):
    route_ops = _load_route_ops()
    monkeypatch.setattr(route_ops, "rebuild_bitmap_from_iptables", lambda bitmap: 0)
    monkeypatch.setattr(route_ops, "release_port_and_dnat", lambda *args: None)

    class Writer:
        def del_route(self, tenant_id):
            return False

    monkeypatch.setattr(route_ops, "_cli_redis_writer", lambda: Writer())

    assert route_ops._cli_delete_route("tenant-1", 10001, "10.0.0.2", 0) == 1


def test_remove_all_does_not_hide_iptables_errors(monkeypatch):
    route_ops = _load_route_ops()
    monkeypatch.setattr(
        route_ops,
        "_run_iptables",
        lambda args: CompletedProcess(args, 4, "", "xtables lock unavailable"),
    )

    try:
        route_ops.dnat_remove_all(10000, "10.0.0.2")
    except RuntimeError as exc:
        assert "DNAT check failed" in str(exc)
    else:
        raise AssertionError("iptables errors must fail delete-route")


def test_control_plane_no_longer_creates_scheme_a_dnat():
    source = (
        ROOT / "deploy" / "lambda" / "api" / "core" / "ssm_dispatch.py"
    ).read_text()
    launch_body = source.split("def _launch_vm(", 1)[1].split(
        "\ndef _ssm_send(", 1
    )[0]
    assert "iptables" not in launch_body
    assert "launch-vm.sh" in launch_body


def test_delete_route_is_a_retryable_hard_gate():
    source = (
        ROOT / "deploy" / "lambda" / "api" / "services" / "tenant_service.py"
    ).read_text()
    delete_body = source.split("def delete_tenant(", 1)[1].split(
        "\ndef _reserve_migration_slot(", 1
    )[0]
    assert "route_ops.py delete-route" in delete_body
    assert "route cleanup failed (bitmap/DNAT/Redis)" in delete_body
    assert "_mark_delete_retryable()" in delete_body
    assert "route_ops.py del-route" not in delete_body


def test_missing_host_with_route_state_cannot_finalize_delete():
    path = (
        ROOT / "deploy" / "lambda" / "api" / "services" / "tenant_service.py"
    )
    requires_host = _load_function(path, "_route_cleanup_requires_host")

    assert requires_host({"host_port": 10001, "guest_ip": "10.0.0.2"})
    assert requires_host({"host_private_ip": "10.0.1.2"})
    assert not requires_host({"host_id": "i-123", "host_port": 10001})
    assert not requires_host({"status": "creating"})

    delete_body = path.read_text().split("def delete_tenant(", 1)[1].split(
        "\ndef _reserve_migration_slot(", 1
    )[0]
    gate = delete_body.index("if _route_cleanup_requires_host(item):")
    finalize = delete_body.index("# Go-live C: reclaim")
    assert gate < finalize
    assert '"requires_intervention": True' in delete_body
    assert "_mark_delete_retryable()" in delete_body[gate:finalize]


def test_tenant_stats_response_preserves_json_number_types(monkeypatch):
    class Table:
        def get_item(self, **kwargs):
            return {
                "Item": {
                    "id": "current",
                    "data_as_of": "2099-01-01T00:00:00Z",
                    "business": {
                        "total": Decimal("2"),
                        "running": Decimal("1"),
                    },
                    "status_counts": [
                        {"status": "running", "count": Decimal("1")}
                    ],
                    "ratio": Decimal("0.5"),
                }
            }

    service = _load_tenant_stats_service(monkeypatch, Table())
    response = service.get_tenant_stats({})
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["business"] == {"total": 2, "running": 1}
    assert body["status_counts"][0]["count"] == 1
    assert isinstance(body["business"]["total"], int)
    assert isinstance(body["ratio"], float)


def test_monitor_artifacts_match_manifest_hashes():
    patch_dir = ROOT / "patch" / "monitor-patch"
    manifest = json.loads((patch_dir / "manifest.json").read_text())
    artifacts = [
        entry for entry in manifest["paths"].values() if entry["artifact"] is not None
    ]

    assert len(artifacts) == 23
    for entry in artifacts:
        actual = hashlib.sha256((patch_dir / entry["artifact"]).read_bytes()).hexdigest()
        assert actual == entry["patch_sha256"]


def test_layered_353_then_376_state_matches_monitor_base():
    patch_root = ROOT / "patch"
    monitor = json.loads((patch_root / "monitor-patch" / "manifest.json").read_text())
    patch_353 = json.loads(
        (
            patch_root / "353-secret-ttl-plus-post315-rollup" / "manifest.json"
        ).read_text()
    )
    patch_376 = json.loads(
        (patch_root / "376-create-image-snapshot" / "manifest.json").read_text()
    )

    checked = 0
    for path, entry in monitor["paths"].items():
        if entry["artifact"] is None:
            continue
        if path in patch_376["paths"]:
            predecessor_hash = patch_376["paths"][path]["patch_sha256"]
        elif path in patch_353["paths"]:
            predecessor_hash = patch_353["paths"][path]["patch_sha256"]
        else:
            try:
                content = check_output(
                    ["git", "show", f"{patch_353['base_sha']}:{path}"],
                    cwd=ROOT,
                    stderr=-3,
                )
            except CalledProcessError:
                predecessor_hash = None
            else:
                predecessor_hash = hashlib.sha256(content).hexdigest()
        assert predecessor_hash == entry["base_sha256"], path
        checked += 1

    assert checked == 23


def test_edge_fluent_bit_inputs_are_injected_and_fail_closed():
    stack = (ROOT / "deploy" / "stacks" / "ha_edge.py").read_text()
    installer = (
        ROOT / "deploy" / "edge" / "fluent-bit" / "install-fluent-bit.sh"
    ).read_text()

    assert 'FIREHOSE_DELIVERY_STREAM="claw-logs{self._gsuffix}"' in stack
    assert 'ASSETS_BUCKET="{assets_bucket.bucket_name}"' in stack
    assert 'AWS_REGION="{self.region}"' in stack
    assert "unresolved FB_* placeholder(s)" in installer
    assert "delivery_stream rendered empty" in installer
    assert "--dry-run" in installer
    assert "systemctl is-active --quiet fluent-bit" in installer


def test_monitor_runbook_updates_the_independent_lifecycle_consumer():
    patch_dir = ROOT / "patch" / "monitor-patch"
    runbook = (patch_dir / "APPLY-INSTRUCTIONS.md").read_text()
    manifest = json.loads((patch_dir / "manifest.json").read_text())

    assert 'test -n "${BASH_VERSION:-}"' in runbook
    assert "LIFECYCLE_QUEUE_ARN" in runbook
    assert "LIFECYCLE_ESM_UUID" in runbook
    assert "/tmp/lifecycle.before.zip" in runbook
    assert "/tmp/lifecycle.patched.zip" in runbook
    assert "/tmp/api-resources.route-current.json" in runbook
    assert 'select(.path == "/tenants-stats")' in runbook
    assert 'OPTIONS_RESPONSE_INPUT=$(jq -nc' in runbook
    assert '--function-name "$LIFECYCLE_FN"' in runbook
    assert "update-event-source-mapping" in runbook
    assert "curl -fsS http://127.0.0.1:8899/metrics >/dev/null && break" in runbook

    api_entries = [
        entry
        for path, entry in manifest["paths"].items()
        if path.startswith("deploy/lambda/api/") and entry["layer"] == "C-lambda"
    ]
    assert len(api_entries) == 11
    assert all(
        "lifecycle consumer" in entry["operations"][0]["resource"]
        for entry in api_entries
    )


def test_monitor_runbook_uses_rendered_edge_paths_and_fails_closed():
    runbook = (
        ROOT / "patch" / "monitor-patch" / "APPLY-INSTRUCTIONS.md"
    ).read_text()

    assert "/opt/openclaw-edge/install-edge.sh" in runbook
    assert "/usr/local/openresty/nginx/sbin/nginx -t" in runbook
    assert "systemctl is-active --quiet claw-edge.service" in runbook
    assert "compared directly with the source artifact hash" in runbook
    assert "A node containing only `/opt/monitoring/.env`" in runbook
    assert "test -f /opt/monitoring/docker-compose.prom-grafana.yml" in runbook
