import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "patch" / "353-secret-ttl-plus-post315-rollup"
SCRIPT = KIT / "lib" / "apply-api-routes.sh"
INSTRUCTIONS = KIT / "APPLY-INSTRUCTIONS.md"


def _write_fake_aws(path: Path):
    path.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail
get_arg() {
  local name="$1"
  shift
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "$name" ]; then printf '%s\n' "$2"; return; fi
    shift
  done
}
case "$1 $2" in
  "apigateway get-resources")
    printf '%s\n' '{"items":[
      {"id":"root","path":"/"},
      {"id":"host","path":"/hosts/{instance_id}"},
      {"id":"img","path":"/images"},
      {"id":"r1","path":"/list_image_versions"},
      {"id":"r2","path":"/hosts/{instance_id}/pull-image-progress"},
      {"id":"r3","path":"/hosts/{instance_id}/copy-file-from-s3"}
    ]}'
    ;;
  "apigateway get-method")
    method=$(get_arg --http-method "$@")
    if [ "$method" = OPTIONS ]; then
      printf '%s\n' '{"authorizationType":"NONE","apiKeyRequired":false,
        "methodResponses":{"204":{"statusCode":"204"}}}'
    else
      printf '%s\n' '{"authorizationType":"NONE","apiKeyRequired":true}'
    fi
    ;;
  "apigateway get-integration")
    method=$(get_arg --http-method "$@")
    rid=$(get_arg --resource-id "$@")
    if [ "$method" = OPTIONS ]; then
      printf '%s\n' '{"type":"MOCK","integrationResponses":{"204":{"statusCode":"204"}}}'
    elif [ "${FAKE_BAD_URI:-0}" = 1 ] && [ "$rid" = r2 ]; then
      printf '%s\n' '{"type":"AWS_PROXY","httpMethod":"POST","uri":"arn:wrong"}'
    else
      printf '%s\n' '{"type":"AWS_PROXY","httpMethod":"POST","uri":"arn:lambda:live",
        "passthroughBehavior":"WHEN_NO_MATCH","timeoutInMillis":29000}'
    fi
    ;;
  "apigateway get-stage")
    printf '%s\n' '{"deploymentId":"dep-live"}'
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 99
    ;;
esac
"""
    )
    path.chmod(0o755)


def _verify(tmp_path: Path, *, bad_uri: bool = False):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_aws(fake_bin / "aws")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "OC_APPLY_API_STATE_DIR": str(tmp_path / "state"),
        "FAKE_BAD_URI": "1" if bad_uri else "0",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "verify", "api123", "v1", "us-east-1"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_verify_accepts_matching_routes_and_cors(tmp_path):
    result = _verify(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("PASS:") == 4
    assert "POST /hosts/{instance_id}/copy-file-from-s3 + OPTIONS" in result.stdout
    assert "stage 'v1' deployment=dep-live" in result.stdout


def test_verify_fails_closed_on_integration_drift(tmp_path):
    result = _verify(tmp_path, bad_uri=True)

    assert result.returncode == 2
    assert "integration differs from GET /images" in result.stderr


def test_docs_cover_full_route_lifecycle():
    instructions = INSTRUCTIONS.read_text()
    script = SCRIPT.read_text()

    for verb in ("plan", "apply", "verify", "rollback"):
        assert f"apply-api-routes.sh {verb}" in instructions
    for path in (
        "/list_image_versions",
        "/hosts/{instance_id}/pull-image-progress",
        "/hosts/{instance_id}/copy-file-from-s3",
    ):
        assert path in script
    assert "put-method-response" in script
    assert "put-integration-response" in script
    assert "previous_deployment" in script
    assert "delete-resource" in script
