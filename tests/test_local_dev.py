# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for local development mode (issue #24).

The local-dev/ directory ships a docker-compose stack that mocks AWS
(LocalStack) and runs the orchestrator Lambda + a stub host-agent on
the host. Contributors can iterate without deploying to AWS.

We don't `docker compose up` in unit tests (that's an integration
concern), but we *can* assert:

1. The directory + scripts exist and are executable.
2. The compose file is valid YAML and declares the expected services.
3. The .env.example documents the required local credentials.
4. The README has a quick-start.
"""

import os
import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = ROOT / "local-dev"


@pytest.mark.unit
class TestModuleLayout:
    def test_local_dev_dir_exists(self):
        assert DEV_DIR.is_dir(), "local-dev/ directory missing"

    @pytest.mark.parametrize("filename", [
        "docker-compose.yml", ".env.example",
        "start.sh", "stop.sh", "README.md",
    ])
    def test_required_file_exists(self, filename):
        f = DEV_DIR / filename
        assert f.is_file(), f"local-dev/{filename} missing"
        assert f.stat().st_size > 0, f"local-dev/{filename} is empty"

    @pytest.mark.parametrize("script", ["start.sh", "stop.sh"])
    def test_scripts_are_executable(self, script):
        f = DEV_DIR / script
        mode = f.stat().st_mode
        assert mode & stat.S_IXUSR, f"local-dev/{script} not executable"


@pytest.mark.unit
class TestComposeStructure:
    def test_compose_is_valid_yaml(self):
        text = (DEV_DIR / "docker-compose.yml").read_text()
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict), "compose file must be a YAML mapping"
        assert "services" in parsed

    def test_localstack_service_present(self):
        compose = yaml.safe_load((DEV_DIR / "docker-compose.yml").read_text())
        services = compose.get("services", {})
        assert "localstack" in services, "localstack service missing"
        # localstack runs DDB, S3, Lambda, IAM, SSM, etc.
        env = services["localstack"].get("environment", [])
        env_str = str(env).lower()
        assert "dynamodb" in env_str or "services" in env_str

    def test_host_agent_stub_service(self):
        compose = yaml.safe_load((DEV_DIR / "docker-compose.yml").read_text())
        services = compose.get("services", {})
        # Either a python:slim image running our stub, or a custom build.
        assert "host-agent" in services or "agent" in services, \
            "host-agent stub service missing"


@pytest.mark.unit
class TestEnvExample:
    def test_documents_aws_endpoint(self):
        text = (DEV_DIR / ".env.example").read_text()
        assert "LOCALSTACK" in text or "AWS_ENDPOINT" in text, \
            ".env.example must document the LocalStack endpoint URL"

    def test_does_not_contain_real_credentials(self):
        text = (DEV_DIR / ".env.example").read_text()
        # Defensive: never ship a real-looking AKIA in the example
        assert "AKIA" not in text or "test" in text.lower(), \
            ".env.example looks like it has real credentials"


@pytest.mark.unit
class TestReadme:
    def test_quick_start_present(self):
        text = (DEV_DIR / "README.md").read_text().lower()
        # Must reference start.sh + docker compose so newcomers know what to run
        assert "./start.sh" in text or "docker compose" in text or "docker-compose" in text

    def test_localstack_documented(self):
        text = (DEV_DIR / "README.md").read_text().lower()
        assert "localstack" in text
