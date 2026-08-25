# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#265 — the observability S3 assets ship with ``cdk deploy``, not with setup.sh.

``deployment/observability/`` is read at boot by ``install-fluent-bit.sh`` (the
whole role prefix, recursively) and by ``init-host.sh`` (the installer plus the
ADOT config). Until this module those ten objects were pushed by a hand-run block
in ``setup.sh``, which produced three separate incidents:

* **#258** — the prefix was missing the installer and the whole ``host/`` subtree,
  so ``init-host.sh`` exited 1, no host ever got Fluent Bit, and OpenSearch was empty.
* **#458 (P0)** — a patch hot-fixed ``/etc/fluent-bit`` on the running fleet but never
  promoted to this prefix, so every newly launched host came up on the pre-patch
  config: fixed until the next boot, then silently back.
* **#531** — measured again 2026-08-19: the in-service prefix still served the
  pre-#531 installer (``git-commit dff19f2b``, 7571 B, zero guard hits) while
  ``gitlab/bb`` had ``ab053a84``/11400 B. The code had been merged for a day; nobody
  re-ran the upload block.

Note the asymmetry that makes this prefix, and not ``deployment/scripts/``, the one
that keeps failing: ``setup.sh``'s ``_REQUIRED_SCRIPTS`` gate greps the bucket after
uploading and exits non-zero on a miss — but it only covers ``deployment/scripts/``.
The observability prefix had no gate at all, so "forgot to upload" was silent there
and loud everywhere else.

Two properties of the fix are not obvious, and both are why this is ten
``BucketDeployment`` constructs instead of one:

1. **Per-object metadata has a real consumer.** ``console-bff/handler.mjs`` echoes
   ``sha256`` / ``uploaded-at`` / ``git-commit`` per object (list items, ``X-Obs-*``
   response headers, single-object JSON), and its tests assert two objects report
   *different* shas. ``BucketDeployment``'s ``metadata`` is documented as being set on
   *all objects in the deployment*, so a single deployment covering the prefix
   structurally cannot carry a per-object digest — it would blank the version echo.
   One object per deployment makes "all objects" degenerate into "this object".

2. **Upload order is load-bearing.** ``fluent-bit.conf`` names its role's parsers and
   Lua filters by path, and ``install-fluent-bit.sh`` dies when a referenced script is
   absent — a die inside the lifecycle hook is an ASG ABANDON, not a retry. So a host
   booting after the new conf landed but before its new Lua did would take the fleet
   down. ``setup.sh`` handled this by ordering its ``aws s3 cp`` calls; here the
   ordering is a CloudFormation dependency edge, which holds even when the deployments
   run concurrently. The reverse pairing (new Lua, old conf) is safe: the old conf
   simply does not reference the new file.

What this does *not* buy: the ten objects live at fixed keys, so a deploy is still not
atomic across the set — a boot interleaved mid-deploy can mix generations that are each
self-consistent per role but not necessarily matched to the installer. Closing that
needs a content-addressed prefix like ``deployment/bootstrap/host/<sha>/`` (see
``ha_edge.py``'s ``HostInitAssetDeployment``), which also requires changing every
consumer's hardcoded prefix. That is deliberately out of #265's DoD.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

# aws_cdk is imported inside build_obs_assets, not here. The inventory below is the
# (.gitlab-ci.yml's pytest-highrisk job) installs pytest but not aws-cdk-lib — a
# module-level CDK import would make every assertion about this list skip silently,
# which is the "green gate, zero assertions" failure that same job's comments warn about.

# The prefix the consumers hardcode: install-fluent-bit.sh:101 (`_s3_prefix`),
# init-host.sh:399 (ADOT) and init-host.sh:481 (the installer).
OBS_PREFIX = "deployment/observability"

# objects the fleet needs drifted from the list something uploads", so the stack and
# the tests read the same tuple rather than each keeping its own copy.
#
# (construct id, repo-relative source, key under OBS_PREFIX)
OBS_ASSETS: tuple[tuple[str, str, str], ...] = (
    ("ObsAdotConfig", "deploy/userdata/adot-config.yaml", "adot/adot-config.yaml"),
    (
        "ObsFbInstaller",
        "deploy/edge/fluent-bit/install-fluent-bit.sh",
        "fluent-bit/install-fluent-bit.sh",
    ),
    # edge role
    (
        "ObsFbEdgeParsers",
        "deploy/edge/fluent-bit/edge/parsers.conf",
        "fluent-bit/edge/parsers.conf",
    ),
    (
        "ObsFbEdgeTraceRoot",
        "deploy/edge/fluent-bit/edge/extract_trace_root.lua",
        "fluent-bit/edge/extract_trace_root.lua",
    ),
    (
        "ObsFbEdgeTimestamp",
        "deploy/edge/fluent-bit/edge/add_timestamp.lua",
        "fluent-bit/edge/add_timestamp.lua",
    ),
    (
        "ObsFbEdgeConf",
        "deploy/edge/fluent-bit/edge/fluent-bit.conf",
        "fluent-bit/edge/fluent-bit.conf",
    ),
    (
        "ObsFbHostParsers",
        "deploy/edge/fluent-bit/host/parsers.conf",
        "fluent-bit/host/parsers.conf",
    ),
    (
        "ObsFbHostTenantId",
        "deploy/edge/fluent-bit/host/extract_tenant_id.lua",
        "fluent-bit/host/extract_tenant_id.lua",
    ),
    (
        "ObsFbHostTimestamp",
        "deploy/edge/fluent-bit/host/add_timestamp.lua",
        "fluent-bit/host/add_timestamp.lua",
    ),
    (
        "ObsFbHostConf",
        "deploy/edge/fluent-bit/host/fluent-bit.conf",
        "fluent-bit/host/fluent-bit.conf",
    ),
)

# A role's fluent-bit.conf must not be visible before the files it references.
# See the module docstring: conf-before-script is an installer die inside the
# lifecycle hook, i.e. ABANDON, while script-before-conf is inert.
OBS_CONF_DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "ObsFbEdgeConf": ("ObsFbEdgeParsers", "ObsFbEdgeTraceRoot", "ObsFbEdgeTimestamp"),
    "ObsFbHostConf": ("ObsFbHostParsers", "ObsFbHostTenantId", "ObsFbHostTimestamp"),
}


BUILD_INFO_NAME = ".oc-build-info"


def obs_provenance(repo_root: Path) -> tuple[str, str]:
    """``(git-commit, uploaded-at)`` for the metadata echo, deterministically.

    Deliberately not a synth wall-clock: the value lands in a custom-resource property,
    so a timestamp would mint a diff on every ``cdk diff``/``deploy``, re-upload all ten
    objects for no change, and leave the prefix permanently "drifted" to #518's drift
    detector. It also churns a new S3 version per object per deploy, which #217's
    ``openclaw-version-snapshots`` records by exact VersionId.

    A commit date alone is not enough either, and this was measured rather than reasoned:
    the real deploy path tars the repo *without* ``.git`` and synthesises on a bastion, so
    ``git`` there answers for nothing. With only an env-var override, a plain
    ``cdk diff``/``cdk deploy`` on the deploy machine resolved to ``unknown`` and reported
    all ten deployments as changed — the same instability the wall clock would have caused.
    Measured 2026-08-19 on the us-east-2 testbed: ``.git-commit: 6f962c22 -> unknown``.

    So the value has to travel *with the artifact*. Resolution order, first hit wins:

    1. ``OC_OBS_GIT_COMMIT`` / ``OC_OBS_UPLOADED_AT`` — an explicit operator override.
    2. ``<repo_root>/.oc-build-info`` — written by ``clawpool-deploy.sh`` before it packs
       the tree, so every synth from that artifact answers identically, with or without
       ``.git`` and with or without the env.
    3. ``git`` in ``repo_root`` — a developer's own checkout.
    4. ``unknown`` — mirrors what ``setup.sh``'s ``_OBS_COMMIT`` fell back to. Stable, so a
       tarball with no provenance at least does not flap between deploys.
    """
    commit = os.environ.get("OC_OBS_GIT_COMMIT", "").strip()
    at = os.environ.get("OC_OBS_UPLOADED_AT", "").strip()

    if not commit or not at:
        info = repo_root / BUILD_INFO_NAME
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            commit = commit or str(data.get("git_commit") or "").strip()
            at = at or str(data.get("committed_at") or "").strip()

    if not commit or not at:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_root), "show", "-s", "--format=%h %cI", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            out = []
        if len(out) == 2:
            commit = commit or out[0]
            at = at or out[1]

    return (commit or "unknown", at or "unknown")


def build_obs_assets(scope, assets_bucket, repo_root: Path) -> list:
    """Deploy ``OBS_PREFIX`` from the repo and return the constructs to gate on.

    Callers must make every ASG whose boot reads this prefix depend on the returned
    list. Without that edge the objects and the first instance race, and a lost race
    is the #258 shape: installer absent, ``init-host.sh`` exit 1, ABANDON.
    """
    from aws_cdk import aws_s3_deployment as s3deploy

    commit, uploaded_at = obs_provenance(repo_root)
    built: dict[str, object] = {}
    for construct_id, rel_src, rel_key in OBS_ASSETS:
        src = repo_root / rel_src
        # which is the whole point: fail on the deploy machine, not on the fleet.
        body = src.read_bytes()
        name = rel_key.rsplit("/", 1)[-1]
        sub_prefix = rel_key.rsplit("/", 1)[0] if "/" in rel_key else ""
        built[construct_id] = s3deploy.BucketDeployment(
            scope,
            construct_id,
            sources=[s3deploy.Source.data(name, body.decode("utf-8"))],
            destination_bucket=assets_bucket,
            destination_key_prefix=f"{OBS_PREFIX}/{sub_prefix}" if sub_prefix else OBS_PREFIX,
            # prune=True (the default) deletes everything under the destination
            # prefix that this deployment did not put there. Ten deployments share
            # two role prefixes, so the default would have them delete each other's
            # objects and leave whichever ran last as the only survivor.
            prune=False,
            retain_on_delete=True,
            metadata={
                "sha256": hashlib.sha256(body).hexdigest(),
                "uploaded-at": uploaded_at,
                "git-commit": commit,
            },
        )
    for conf_id, dep_ids in OBS_CONF_DEPENDS_ON.items():
        for dep_id in dep_ids:
            built[conf_id].node.add_dependency(built[dep_id])
    return list(built.values())
