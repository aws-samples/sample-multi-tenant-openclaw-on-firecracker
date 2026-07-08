# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static deployment-manifest regression test (issue #64 acceptance criterion).

History
-------
Live migration (#20/#45) and disk-resize (#22) both shipped the API surface
but the host-side shell script was *never deployed*:

  - ``deploy/userdata/migrate-vm.sh`` existed in source but was missing from
    BOTH ``setup.sh`` (S3 upload) and ``init-host.sh`` (host download), so
    every ``SSM SendCommand "/home/ubuntu/migrate-vm.sh ..."`` hit a missing
    file and failed with ``exit 127``. The Lambda didn't check the SSM result,
    so the bug was silent for ~1 year (issue #64).
  - ``resize-disk.sh`` had the identical defect, undetected.

Every existing unit test mocked the SSM layer, so none of them could ever
catch "script referenced by a Lambda but not actually shipped to the host".

This test closes that gap with pure static analysis — no AWS, no mocks.

What it asserts
---------------
For every ``/home/ubuntu/<name>.sh`` invoked by any Lambda handler:

  1. The source script ``deploy/userdata/<name>.sh`` exists.
  2. ``setup.sh`` uploads it to ``s3://.../deployment/scripts/<name>.sh``.
  3. It is delivered to the host — either by an explicit ``init-host.sh``
     download line, OR via the ``{{BACKUP_DATA_SCRIPT}}`` template that
     ``deploy/stack.py`` renders into ``init-host.sh`` at synth time.

If any of these fail, a future "API shipped without data-plane wiring"
regression breaks CI instead of production.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_DIR = ROOT / "deploy" / "lambda"
USERDATA_DIR = ROOT / "deploy" / "userdata"
SETUP_SH = ROOT / "setup.sh"
INIT_HOST_SH = USERDATA_DIR / "init-host.sh"
STACK_PY = ROOT / "deploy" / "stack.py"

# Matches /home/ubuntu/<name>.sh — the canonical path Lambdas invoke via SSM.
_HOST_SCRIPT_RE = re.compile(r"/home/ubuntu/([A-Za-z0-9_-]+\.sh)")


def _lambda_referenced_scripts():
    """Every <name>.sh invoked as /home/ubuntu/<name>.sh by any Lambda handler.

    Reads only handler.py source (not __pycache__) so the result reflects the
    code that will actually be deployed.
    """
    found = set()
    for handler in LAMBDA_DIR.rglob("handler.py"):
        text = handler.read_text(encoding="utf-8")
        for m in _HOST_SCRIPT_RE.finditer(text):
            found.add(m.group(1))
    return found


def _rendered_init_host_text():
    """init-host.sh as the host will actually see it: the raw template with
    every ``{{PLACEHOLDER}}`` that stack.py rewrites expanded inline.

    Currently only ``{{BACKUP_DATA_SCRIPT}}`` injects a script download; we
    expand it from stack.py's own replacement string so the test tracks the
    real rendering logic rather than hard-coding backup-data.sh.
    """
    init_text = INIT_HOST_SH.read_text(encoding="utf-8")
    stack_text = STACK_PY.read_text(encoding="utf-8")
    # Pull the literal that stack.py substitutes for {{BACKUP_DATA_SCRIPT}}.
    # It is a multi-line .replace("{{BACKUP_DATA_SCRIPT}}", "....") — grab any
    # /home/ubuntu/*.sh download lines that appear near that replacement.
    for m in re.finditer(r'\{\{BACKUP_DATA_SCRIPT\}\}"\s*,\s*\n?(.*?)\)', stack_text, re.DOTALL):
        init_text += "\n" + m.group(1)
    # Fallback: also append any stack.py line that downloads a deployment
    # script, so future template-injected scripts are likewise recognised.
    for line in stack_text.splitlines():
        if "deployment/scripts/" in line and "/home/ubuntu/" in line:
            init_text += "\n" + line
    return init_text


LAMBDA_SCRIPTS = sorted(_lambda_referenced_scripts())


@pytest.mark.regression
def test_lambda_referenced_scripts_discovered():
    """Sanity: we actually found the known host scripts. Guards against the
    regex silently matching nothing (which would make every other assert pass
    vacuously)."""
    assert LAMBDA_SCRIPTS, "no /home/ubuntu/*.sh references found in any Lambda handler"
    # These are the load-bearing data-plane scripts; if any disappears from the
    # Lambda code this list should be updated deliberately.
    for expected in ("launch-vm.sh", "stop-vm.sh", "migrate-vm.sh", "resize-disk.sh"):
        assert expected in LAMBDA_SCRIPTS, (
            f"{expected} no longer referenced by any Lambda — intended?"
        )


@pytest.mark.regression
@pytest.mark.parametrize("script", LAMBDA_SCRIPTS)
def test_source_script_exists(script):
    """The script a Lambda invokes must exist in deploy/userdata/."""
    assert (USERDATA_DIR / script).is_file(), (
        f"{script} is invoked by a Lambda but deploy/userdata/{script} does not exist"
    )


@pytest.mark.regression
@pytest.mark.parametrize("script", LAMBDA_SCRIPTS)
def test_script_uploaded_by_setup(script):
    """setup.sh must upload every Lambda-referenced script to S3.

    This is the assertion that would have failed on migrate-vm.sh (issue #64)
    and resize-disk.sh the moment either API was wired up.
    """
    setup_text = SETUP_SH.read_text(encoding="utf-8")
    needle = f"deployment/scripts/{script}"
    assert needle in setup_text, (
        f"{script} is invoked by a Lambda but setup.sh never uploads it to "
        f"s3://.../deployment/scripts/{script} — it will be missing on every "
        f"host and the SSM command will fail with exit 127 (see issue #64)."
    )


@pytest.mark.regression
@pytest.mark.parametrize("script", LAMBDA_SCRIPTS)
def test_script_delivered_to_host(script):
    """Every Lambda-referenced script must reach /home/ubuntu/ on the host,
    via init-host.sh directly or via a stack.py-rendered template block."""
    rendered = _rendered_init_host_text()
    needle = f"deployment/scripts/{script}"
    assert needle in rendered, (
        f"{script} is uploaded to S3 but no init-host.sh / stack.py template "
        f"line downloads it to the host — the host will not have "
        f"/home/ubuntu/{script}."
    )
