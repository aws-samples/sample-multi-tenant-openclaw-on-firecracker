# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic single-object bundle of deploy/edge/ (#389 v2 block 4).

The edge fleet used to bootstrap by ``aws s3 cp --recursive`` from a mutable
``deployment/edge/`` prefix that ``setup.sh`` uploaded by hand, polling 60x10s for
``install-edge.sh`` to appear. Two failure modes came out of that: a patch that forgot the
upload left the prefix stale and the change silently never took effect, and a half-finished
upload could be consumed as if complete because nothing verified the set.

Host solved the same problem in v1 by binding one immutable digest-addressed object into
the LaunchTemplate. ``deploy/edge/`` is a tree rather than a single file, so it is packed
into one tar.gz and that object carries the digest — one artifact, one sha256, verified
before anything is unpacked.

Base64 rather than raw bytes because the publisher is ``s3deploy.Source.data``, which takes
text. That is a deliberate constraint, not an accident: BucketDeployment is what makes
CloudFormation stage the object *before* the edge ASG is allowed to launch, which is the
race the polling loop existed to paper over.

Every field a tar records but the repo does not (mtime, uid/gid, owner names, member order)
is pinned, because the digest ends up inside the LaunchTemplate user-data: a nondeterministic
tar would mint a new LT version on every ``cdk deploy`` and churn the edge fleet for no
change at all.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
import textwrap
from pathlib import Path

# Lua specs and their harness. They are developer tooling; a running edge never reads them,
# and shipping them would put test doubles on a production node.
_EXCLUDE_DIRS = frozenset({"test"})

BUNDLE_OBJECT_NAME = "edge-bundle.tar.gz.b64"
_B64_LINE_WIDTH = 76


def _bundle_members(root: Path) -> list[tuple[str, bytes]]:
    """Return (relative posix path, bytes) for every file that belongs on an edge node."""
    members = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _EXCLUDE_DIRS & set(rel.parts[:-1]):
            continue
        members.append((rel.as_posix(), path.read_bytes()))
    # Sorted, because rglob order follows the filesystem and would otherwise leak the
    # build machine's directory layout into the digest.
    return sorted(members)


def build_edge_bundle(root: Path) -> tuple[str, str]:
    """Pack ``root`` into a base64 tar.gz and return (text, sha256 of that text).

    The digest is over the base64 text, i.e. over exactly the bytes the instance downloads
    and pipes to ``sha256sum -c``. Hashing the inner tar instead would leave the transport
    encoding unverified.
    """
    tar_buf = io.BytesIO()
    # GNU_FORMAT: PAX would add extended headers whose content depends on the writer.
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in _bundle_members(root):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            # git tracks one bit of mode, so derive the rest rather than copying the
            # checkout's: a developer's umask must not change what the fleet runs.
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))

    gz_buf = io.BytesIO()
    # mtime=0 for the same reason as above; gzip stamps the current time by default.
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(tar_buf.getvalue())

    text = (
        "\n".join(
            textwrap.wrap(
                base64.b64encode(gz_buf.getvalue()).decode("ascii"),
                _B64_LINE_WIDTH,
            )
        )
        + "\n"
    )
    return text, hashlib.sha256(text.encode("ascii")).hexdigest()
