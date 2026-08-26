# edge-balancer-cosocket-606 — apply by reading, no CloudFormation redeploy

> This kit brings the edge data plane to the same revision as the internal source. Two changes
> carry the weight. The Redis coordinates now travel through a single channel established at
> init, which closes a readiness gate that was permanently fail-open — `/healthz` could answer
> 200 without Redis ever having been reached. And the balancer-phase restriction now has a probe
> that runs in the real phase under a real OpenResty, which is the layer a stubbed unit test
> cannot reach. The tenant identifier and utility modules, the log forwarding configuration and
> the installer are brought along so the tree is consistent rather than partly updated. The
> `balancer_by_lua*` cosocket fix itself reached the public tree in an earlier batch; this kit
> does not re-deliver it.

`status: MANUAL_REVIEW`. Three operations are `MANUAL_CLI_REVIEW` and must each be reviewed by a
human before you run them. **Do not run anything that triggers a CloudFormation stack update.**

- `base_sha` = `fae91796206da1d2961d1c5537285278bc0a80f8`
- `patch_sha` = `9e399b7b834822ce02d7dd8f21a0491a9638f113`

The range contains 19 files, all under `deploy/edge/**` — no other layer is pulled in. Both
anchors resolve in the public repository, so every check below is one you can run yourself.

## Four facts that will silently ruin this delivery

**① `nginx.conf` is a template. Do not copy it to the live path.**
The installer renders five placeholders through `envsubst` — `ENGINE_REDIS_HOST`,
`ENGINE_REDIS_PORT`, `ENGINE_REDIS_READER_HOST`, `ENGINE_REDIS_READER_PORT` and
`EDGE_SELF_IP` — and **`EDGE_SELF_IP` differs per instance**, so no single pre-rendered artifact
can be correct for a fleet. Install the raw template and the running configuration keeps a
literal `${ENGINE_REDIS_HOST}`. Since the change being delivered is precisely that the
coordinates now live inside an init block, an unrendered placeholder means the coordinate is a
string and the fix does nothing — while every digest check still passes.

This kit therefore **renders per host, from the same sources the installer uses**. The primary
coordinate comes from `ENGINE_REDIS_ENDPOINT` in `/etc/environment` (written there by the launch
template's user data) and is split into host and port the same way. The reader coordinate is read
from the SSM parameter `/openclaw/engine/redis/reader-endpoint`, and **falls back to the primary
when that parameter is absent, empty or malformed** — which is exactly what the installer does.
`EDGE_SELF_IP` comes from instance metadata. After rendering, the operation **asserts that no
placeholder remains** and that the result contains the init block, and only then installs. If any
value cannot be read it fails loudly rather than guessing. The **unrendered template** is what
goes to the bundle directory, because a later bootstrap renders it itself.

> It deliberately does **not** read the four `ENGINE_REDIS_*_HINT` values from
> `claw-edge.service`. Measured on a real fleet: an older bundle generation writes only
> `HOST` and `PORT`, with no reader pair, so requiring four would make this kit unusable on that
> generation. `ENGINE_REDIS_ENDPOINT` is present in every generation.

**② Files land first; the reload happens once, and the guard is mechanical rather than advisory.**
The module cache keeps already-started workers on the modules they have already required, so a
file on disk that has not been reloaded **changes nothing about live behaviour**. That is the
atomicity guarantee: a partial install is safe, and the risk exists only at the reload. The reload
operation therefore **asserts, on every host, that every file this kit installs is in place** —
the four Lua digests, plus the live `nginx.conf` having no residual placeholder and containing the
init block — before it will proceed. Miss one and the reload produces an immediate
`attempt to call a nil value`, or a coordinate that is a literal string and a warmup that falls
back to its no-coordinate branch.

**③ New instances will not inherit this fix. That is not an open question; it is settled in code.**
The whole edge tree is packed into a **single digest-addressed object**,
`deployment/bootstrap/edge/<sha256>/edge-bundle.tar.gz.b64`, and the launch template's user data
**inlines that same sha256 and runs `sha256sum -c` against it** before unpacking to
`/opt/openclaw-edge/<sha256>/`. So:

- Replacing the bytes of that object **cannot** make new instances inherit anything — the digest
  would not match and the instance would refuse to start
- Inheriting the fix requires a **new bundle version and a new launch template version**, which is
  a deployment-level action
- This kit stops the bleeding on the live fleet only. Any scale-out, health-check replacement or
  availability-zone rebalance brings up an edge that still carries the defect

Step 5 is a **decision gate** for exactly this, and it will not accept silence.

**④ Four files go only to the bundle directory and change no live behaviour.**
`install-edge.sh` and the three `fluent-bit/` configurations land in the bundle directory, after
which the running OpenResty and the running fluent-bit **behave exactly as before** — the first is
executed only at bootstrap, the second keeps using its current configuration. And per fact ③,
writing the bundle directory does **not** make new instances inherit either; it matters only if
someone re-runs `install-edge.sh` on that host, and a further user-data run overwrites it with the
original bundle content. Reconfiguring the running fluent-bit is a different task with a different
blast radius and its own verification, and is out of scope here.

## Step 0 — environment, edge instances, baseline

```bash
: "${REGION:?export REGION first}"
: "${ASSETS_BUCKET:?export ASSETS_BUCKET first}"
: "${EDGE_ASG:=openclaw-edge-asg}"
export REPO_ROOT="${REPO_ROOT:-$PWD}"

EDGE_IDS="$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names "$EDGE_ASG" \
  --query 'AutoScalingGroups[0].Instances[?LifecycleState==`InService`].InstanceId' \
  --output text)"
export EDGE_IDS
test -n "$EDGE_IDS" || { echo "no InService edge instances found" >&2; exit 1; }
echo "edge instances: $EDGE_IDS"
```

Step 5 also needs the launch template coordinates:

```bash
EDGE_LT_NAME="$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names "$EDGE_ASG" \
  --query 'AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateName' \
  --output text)"
test "$EDGE_LT_NAME" != None || EDGE_LT_NAME="$(aws autoscaling describe-auto-scaling-groups \
  --region "$REGION" --auto-scaling-group-names "$EDGE_ASG" \
  --query 'AutoScalingGroups[0].LaunchTemplate.LaunchTemplateName' --output text)"
export EDGE_LT_NAME EDGE_LT_VERSION="${EDGE_LT_VERSION:-\$Latest}"
echo "launch template: $EDGE_LT_NAME @ $EDGE_LT_VERSION"
```

**Baseline.** Confirm the bundle directory can be discovered on each host and record what the live
files are now. The bundle unpacks to `/opt/openclaw-edge/<bundle-sha256>/`, **one directory per
version**, so every command in this kit discovers it on the target host and refuses to run if it
cannot. Do not hardcode a path like `/opt/openclaw-edge/lib` anywhere.

If a live file's digest already equals the `patch_sha256` recorded in `manifest.json`, that host
already has that file — skip it rather than reinstalling.

## Step 1 — upload artifacts to S3 (no live effect, fully reversible)

The first half of each path's `operations[0].apply_cli`. Artifacts go to the **new prefix
`deployment/edge-606/`** and overwrite no existing bundle object. Rolling back is deleting them.

## Step 2 — install the four live Lua files to both landing spots (no reload)

`lib/hints.lua`, `lib/tenant.lua`, `lib/utils.lua`, `route.lua`. Each goes to
`/usr/local/openresty/lualib/edge/...` (the runtime load path) and to the discovered bundle
directory. The `.pre-606` backup anchor is created **once** — with a `.absent` marker when the file
did not exist — and a rerun does not overwrite it. Overwrite the anchor and the backup becomes the
already-patched content, which makes rollback useless.

## Step 3 — render and install `nginx.conf` per host (`MANUAL_CLI_REVIEW`)

See fact ①. This is the only operation that changes the content of a live configuration file.
When reviewing it, confirm three things: where the coordinates come from, that `EDGE_SELF_IP` is
each host's own, and that the zero-residual-placeholder assertion sits **before** the install.

## Step 4 — one guarded reload (`MANUAL_CLI_REVIEW`)

The reload operation on `deploy/edge/nginx.conf`. It asserts the four Lua digests and the two
conditions on the live configuration, then runs `openresty -t` and `openresty -s reload`, then
reads journald back for **two** signals:

- no more `API disabled in the context of balancer_by_lua`
- no more `marking ready without probe`

You can run `openresty -t` first, but it **does not load Lua modules** — passing it does not show
that Lua can load. Lua load and runtime failures appear only in journald and `error.log`.

**One consequence worth planning for.** Before this fix the readiness gate was fail-open, so
`/healthz` answered 200 whether or not Redis was reachable. After it, a host that cannot reach
Redis reports itself unready. If the target group health check is what keeps instances in service,
an environment with unreachable Redis will now cycle instances instead of serving quietly broken
ones. Confirm Redis reachability before the reload; that change in behaviour is the point of the
fix, not a regression.

## Step 5 — the new-instance decision gate (`MANUAL_CLI_REVIEW`, read-only)

Read the launch template user data and confirm for yourself that it fetches a digest-addressed
bundle and runs `sha256sum -c` — that is the on-the-spot evidence for fact ③. Then write one line
of conclusion at the end of `edge-inherit-gate.txt`. Verification accepts only these two:

- `DECISION: live-only` — stop the bleeding on the live fleet, schedule the new bundle and launch
  template version separately. **The cost is that between now and that version, every edge that
  comes up carries the defect** — put that in the on-call handover
- `DECISION: reroll-bundle` — do the deployment-level action now. That is outside this kit; follow
  the deployment process

## Step 6 — the four bundle-source-only files

`install-edge.sh` and the three `fluent-bit/` configurations. See fact ④: **no live effect.**

## Step 7 — ten test assets to the repository copy (skippable)

These update a repository copy only and touch no live resource. Among them,
`deploy/edge/test/integration/balancer_phase_integration.sh` asserts, in the **real
`balancer_by_lua*` phase under a real OpenResty**, that no cosocket is opened — which is the gap
the original defect walked through (a stubbed Redis module cannot reproduce a context restriction,
and `openresty -t` does not trigger a runtime phase error either). Skip the whole group if you do
not keep a clone of the repository. Their `apply_cli` installs only when absent and otherwise
asserts the digest, refusing to overwrite content that differs from what this kit recorded.

## Verification

`manifest.json` carries 8 verifications in three phases:

- **Phase A-readonly (6)**: `verify-639-no-marking-ready-without-probe`,
  `verify-639-conf-rendered-and-init-block`, `verify-606-no-api-disabled`,
  `verify-606-live-and-bundle-digests`, `verify-edge-new-instance-decision`,
  `verify-edge-bundle-source-digests`
- **Phase B-lifecycle (1, the core one)**: `verify-606-retry-path-selfheal`
- **Phase B-optional (1)**: `verify-633-probe-fails-when-cosocket-reintroduced`

**A normal request never enters the retry branch.** So "the WebSocket connects" and "conversation
works" verify nothing about that fix. `verify-606-retry-path-selfheal` is the only probe that shows
it working: point a tenant's route descriptor at an unreachable host (record its `host:port`, then
stop that VM), drive a **real WebSocket from outside**, and assert that the next request within the
level-one cache TTL reaches a new peer with no `API disabled`.

`verify-633-probe-fails-when-cosocket-reintroduced` works by contradiction: after the probe passes
as shipped, **deliberately** add a cosocket call in the balancer phase and the probe must go **red**.
Still green after the injection means this probe judges no better than the stubbed test it replaces.

## Traps

| Trap | Why |
|---|---|
| **Treating `nginx.conf` as an ordinary file** | It is a template; five placeholders must be rendered and `EDGE_SELF_IP` differs per instance |
| **Hardcoding `/opt/openclaw-edge/lib`** | The bundle unpacks to `/opt/openclaw-edge/<bundle-sha256>/`, one directory per version; discover it on the host |
| **Expecting new instances to inherit from a bundle-directory write** | New instances fetch a digest-addressed object whose hash the launch template verifies; replacing bytes makes them refuse to start |
| **Reading the four `*_HINT` values from the unit** | An older bundle generation writes only two of them; derive from `ENGINE_REDIS_ENDPOINT` instead |
| **Trusting `openresty -t`** | It does not load Lua modules. Failures appear only in journald and `error.log` |
| **Changing `lualib` but not the bundle directory** | A later `install-edge.sh` run restores the original bundle content |
| **Verifying only a normal request** | A normal request never enters the retry branch, so it cannot verify that fix |
| **Judging edge health by `/healthz`** | What this fixes is that the healthy signal was not trustworthy: before the fix it answered 200 without ever probing Redis |
| **Letting a rerun overwrite the backup anchor** | `.pre-606` would then hold the patched content and rollback would restore nothing |
| **Rolling back to an earlier bundle** | The defect has been present since 2026-07-21; an earlier version reintroduces it |

## Rollback

Every path's `rollback_cli` **preflights first** — it asserts the live content is still what this
patch installed and refuses to act otherwise — then restores from `.pre-606`, removing the file
where a `.absent` marker says it did not exist. After the four Lua files and `nginx.conf` are
restored, run the reload operation's `rollback_cli` once to make the restore take effect.

Rolling back Step 1 means deleting the objects under
`s3://$ASSETS_BUCKET/deployment/edge-606/`.

**Note:** rolling back returns the readiness gate to permanently fail-open — `/healthz` will again
answer 200 without having probed Redis. Roll back only when the delivery itself is the problem
(Lua fails to load, `openresty -t` fails, the process does not come back after a reload).

## What this kit does not fix

- **The `controlUi.allowedOrigins` whitelist.** That check lives in the gateway inside the guest,
  and its value comes from `platform.env` frozen at host boot. It is unrelated to the edge and is a
  host provisioning change
- **Reclaiming Redis route keys.** Route keys are not reclaimed automatically; restoring a tenant
  relies on a new write overwriting them
- **Guest egress policy.** No causal relationship with this defect
- **New-instance inheritance.** See fact ③ — that needs a new bundle and launch template version

## Provenance and test coverage

| Item | Value |
|---|---|
| Internal source | Copied byte-for-byte from one internal commit, file modes taken from the same tree |
| Range | 19 files under `deploy/edge/**`, no other layer |
| Real-phase probe | `deploy/edge/test/integration/balancer_phase_integration.sh`, delivered in Step 7 |

An earlier revision of these instructions said the integration test was not yet in the kit's
coverage. **It now is**, with a falsifiable standard — reintroduce a cosocket call in the balancer
phase and the probe must go red.
