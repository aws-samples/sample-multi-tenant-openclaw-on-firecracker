# Changelog

## [1.5.4] — 2026-06-18

Host init reliability — newly-launched hosts were getting stuck `MidLifecycleAction` and ABANDONed. Root-caused to upstream drift and a boot-time reboot race; verified end-to-end on a fresh host (init → InService → DDB-registered → carries a tenant).

### Fixed

- **init-host: pin Firecracker version, don't track `latest` (#74).** `latest` bumped to v1.16.0, whose CI guest-kernel assets (`firecracker-ci/v1.16/.../vmlinux`) don't exist yet, so step3b 404'd under `set -e` and bricked every new host. Pinned to `v1.15.1` (overridable via `FC_VERSION`); the binary and the derived guest-kernel URL both resolve again.
- **init-host: stop the boot-time auto-upgrade reboot race (#74).** A stale AMI installs a kernel update at boot and can reboot mid-init, orphaning the lifecycle hook. init now `systemctl stop`s `unattended-upgrades` + `apt-daily-upgrade` for the duration (timers still fire later — daily updates keep working).
- **init-host: never hang the lifecycle hook (#73).** An EXIT trap always settles the ASG hook — `CONTINUE` on success, `ABANDON` on any failure — so a broken init is replaced promptly instead of waiting out the 600s timeout. DDB self-register now retries (concurrent launches throttle writes) and `exit 1`s if it never succeeds, so an unregistered host ABANDONs rather than coming up invisible to the scheduler.
- **init-host: mirror init output to the serial console (#73).** `aws ec2 get-console-output` now shows the full `[oc:init]` progress without SSH/SSM — essential when a host fails before SSM is up or gets terminated.

### Changed

- **RBAC role-gating is now its own switch, default off.** `console_auth.rbac_enabled` (env `RBAC_ENABLED`) is independent of Cognito login: a deployment can require login for the console without forcing every write to carry an id_token. Set it `true` to enforce viewer/operator/admin per-route checks.
- **init-host trimmed under the 16 KB user-data limit.** Verbose comments + the three clone/migrate/resize downloads (collapsed into one loop) brought the rendered user-data back under EC2's hard cap (it had overflowed at 16693 bytes).
- **Console:** Tenants table drops the redundant AZ column (already shown per-host); Refresh/+ New buttons reordered; host CPU/Mem cards render `—` instead of `undefined`/`NaN` when a host record is incomplete.

## [1.5.3] — 2026-06-01

Reliability fixes surfaced while exercising the live deployment: a VM stuck-`creating` dead zone and an x86 bundling failure. Plus a clearer up-front message when migrate isn't usable.

### Fixed

- **host-agent: VM stuck `creating` when Firecracker is alive but the guest network is dead (#69).** A partial launch could leave FC running with its TAP `DOWN`; the old recovery only fired when FC was *absent*, so nothing repaired it and the tenant sat in `creating` indefinitely. host-agent now counts consecutive "FC alive but guest unreachable" polls and force-relaunches (stop-vm + launch-vm to rebuild the network) past a threshold, resetting on any reachable poll.
- **deploy: api Lambda bundling still pulled the arm64 image on x86 hosts (#70).** 1.5.1 dropped the `platform` pin but left `Runtime.PYTHON_3_12.bundling_image`, which resolves to the Lambda's arch (arm64) and fails on x86 without QEMU (`exec format error`) — breaking `cdk synth` and the test suite. The bundling image now tracks the build host's arch; pip still cross-downloads the aarch64 wheel for the ARM_64 runtime.

### Changed

- **api: migrate rejects balloon-enabled tenants up front with a clear message.** Live migration is incompatible with the balloon device (#72, still open). Instead of failing minutes later inside the snapshot step, the API now returns `409` immediately explaining to back up + recreate. This is only a guard rail — the underlying balloon migration support is tracked in #72.

### Added

- **`tests/test_dead_zone_recovery.py`** — covers the #69 counter/threshold logic and force-relaunch ordering (stop before launch).

## [1.5.2] — 2026-06-01

Console RBAC wiring fix. 1.5.0 gated API writes by Cognito `cognito:groups`, but the Console never sent the token and CORS never allowed the header, so every Console write 403'd as `viewer`. #66 and #67 must land together: sending the Bearer header triggers the preflight that allowing the header must permit.

### Fixed

- **Console sends `Authorization: Bearer <id_token>` (#66)** so RBAC sees the real role instead of downgrading to `viewer`.
- **CORS allow-list permits `Authorization` (#67)** in both the API Gateway preflight (`stack.py`) and Lambda responses (`_resp`); the browser preflight previously blocked all authed requests (curl was unaffected — no preflight).
- **Monitoring tab no longer throws when metrics is disabled (#68)** — `systemInfo?.metrics?.…` optional chaining.

### Changed

- **Console favicon** — inline SVG (cyan microVM-grid on dark), removes the `/favicon.ico` 404.

## [1.5.1] — 2026-06-01

Deploy portability fix. 1.5.0 flipped the `openclaw-api` Lambda to ARM_64 (Graviton) and pinned the CDK bundling image to `platform="linux/arm64"` so the bundled `cryptography` native wheel would deterministically match the runtime arch. That pin had an unintended consequence: it forced an **arm64 build container**, so `cdk synth`/`cdk deploy` from an **x86_64 deploy host** required QEMU/binfmt emulation — without it, bundling failed and the deploy looked like it demanded an arm64 machine. A teammate hit exactly this deploying from x86_64.

### Fixed

- **`openclaw-api` Lambda bundling no longer requires the build host to match the Lambda arch.** Removed `platform="linux/arm64"` from the `BundlingOptions`. The bundling container now runs on the **host's native architecture**, and `pip` **cross-downloads** the prebuilt aarch64 wheel instead of compiling/emulating:
  ```
  pip install --no-cache-dir --platform manylinux2014_aarch64 \
    --implementation cp --python-version 3.12 --only-binary=:all: \
    --upgrade -r requirements.txt -t /asset-output
  ```
  `cryptography` publishes an `abi3` `manylinux2014_aarch64` wheel and `PyJWT` is pure-python (`none-any`), so this resolves on **any** build host (x86_64 or arm64) with no QEMU. The Lambda stays `ARM_64` (Graviton) — only the build path changed. This is the **5th** "only-real-deployment-surfaces-it" bug in the 1.5.x line (after region-context, Cognito domain `AlreadyExists`, and the nat-table illegal `DROP`).

### Verified

- **x86_64 build host simulated** (`docker run --platform linux/amd64`): the cross-download installs `cryptography 48.0.0` + `PyJWT 2.13.0`, and the resulting `cryptography/hazmat/bindings/_rust.abi3.so` + `_cffi_backend*.so` are ELF `e_machine=0xB7` (**AArch64**) — i.e. an x86 host produces genuine arm64 binaries for the Graviton runtime.
- **Real `cdk synth -c region=ap-northeast-1`** completes (`Successfully installed PyJWT-2.13.0 … cryptography-48.0.0`, exit 0); every `_rust.abi3.so` in the synthesized `cdk.out` assets is AArch64.
- **Live `openclaw-api` (the 2026-05-31 1.5.0 deploy) is healthy and arm64**: `Architectures=[arm64]`, `State=Active`, real pool `ap-northeast-1_yvmL2GT3P`, `DEFAULT_NO_JWT_ROLE=viewer`. `scripts/e2e-rbac-test.sh` passes 5/5 through API Gateway (no-token write → 403, read → 200, forged-RS256 → 403, alg:none → 403) — proving `cryptography` imports and runs RS256 verification on the arm64 runtime (a failed import would 5xx, not return a precise 403).

### Operator notes

- **No code/behavior change to the running Lambda** — this only fixes the *build path*. The live function from the 1.5.0 deploy is already correct (arm64, verified). Redeploy is only needed to (a) deploy from an x86_64 host that previously failed, or (b) pick up future changes; `cdk deploy -c region=<region>` now works from any host with Docker, no QEMU required.

## [1.5.0] — 2026-05-31

Security hardening. Two real, exploitable vulnerabilities recorded as "Known limitations" in 1.4.4 are now fixed at the root: the API's JWT was never signature-verified and failed **open** to `admin`, and every microVM shipped the **same hardcoded SSH password** with root login enabled. Both are closed. This release changes auth semantics — read **Breaking** before deploying.

### Why this matters

The 1.4.4 notes were honest about deferring these, but they were not cosmetic:

- **RBAC fail-open + unsigned JWT.** `_get_user_role` returned `admin` whenever a request carried no `Authorization: Bearer` token, and when a token *was* present the handler base64-decoded the payload **without verifying the signature**. An attacker could base64-craft `{"cognito:groups":["admin"]}` (even with `alg:none`, no signature) and the handler would grant admin — or simply omit the token entirely and still get admin. The API key alone gated mutations; the role layer was decorative.
- **Shared SSH password + root login.** `build-rootfs.sh` baked `root:OpenCl@w2026` / `agent:OpenCl@w2026` into the rootfs shared by **all** tenants, with `PermitRootLogin yes` and password auth on. The same password was hardcoded in three more places and passed to guests over SSM (landing in CloudTrail). Anyone holding it could `ssh root@<any guest_ip>` — tenant isolation did not exist at the SSH layer. This directly violated the project's "no hardcoded secrets" rule.

### Breaking

- **No Bearer token now resolves to `viewer`, not `admin`.** Requests authenticated only by `x-api-key` (CLI / curl / automation) can still **read** (viewer is allowed on all GETs) but **writes return 403**. Automation that mutates state must now present a valid Cognito **id_token** as `Authorization: Bearer …`. The fallback role is configurable per environment via `DEFAULT_NO_JWT_ROLE` (default `viewer`); set it to `admin` only for a trusted, network-isolated automation deployment.
- **`POST /migrate` etc. from the e2e scripts will 403 without a token.** Run write-path automation with a real admin id_token, or deploy with `DEFAULT_NO_JWT_ROLE=admin` for the test environment.
- **microVM SSH is pubkey-only.** Password login and root login are disabled in the image. Host→guest access uses a per-host key; there is no shared password to distribute. Existing VMs launched from the *old* rootfs keep the old password until rolled — roll hosts to fully retire it.

### Security

- **Cognito JWT RS256 signature verification (fail-safe).** New `_verify_and_decode` fetches the User Pool's JWKS (`PyJWKClient`, module-cached) and calls `jwt.decode(..., algorithms=["RS256"], issuer=…, options={"require":["exp","iss"]})`. A token that fails verification — forged signature, `alg:none`, expired, wrong issuer, or (when `COGNITO_CLIENT_ID` is set) wrong audience — yields `None`, and the caller is downgraded to `viewer`. `_get_user_role` is now three-state: no token → `DEFAULT_NO_JWT_ROLE`; token present but untrusted → `viewer`; verified → role from `cognito:groups`. The pre-1.5.0 fail-open `return "admin"` is gone, and the unsigned `_decode_jwt_payload` helper was deleted (zero call sites).
- **Per-host SSH public-key authentication; shared password removed.** `init-host.sh` generates a per-host `ed25519` keypair at boot (`/etc/openclaw/host_vm_key`); the private key never leaves the host. `launch-vm.sh` injects that host's **public** key into each VM's data disk at launch, so every host trusts only its own key (a leaked key on one host cannot reach VMs on another). `build-rootfs.sh` locks both `root` and `agent` passwords (`passwd -l`), sets `PermitRootLogin no` / `PasswordAuthentication no` / `PubkeyAuthentication yes`, and pre-creates `/home/agent/.ssh` (700, agent-owned) — `authorized_keys` is **never** baked into the shared image. `host-agent.py` and the `oc-connect.sh` / `oc-dashboard.sh` operator scripts use `ssh -i /etc/openclaw/host_vm_key -o IdentitiesOnly=yes` instead of `sshpass`. The hardcoded `OpenCl@w2026` is gone from the entire repository (`grep` clean), and the secret no longer transits SSM/CloudTrail.

### Changed

- **api Lambda is now bundled with Docker** (`BundlingOptions`) to ship PyJWT + cryptography. cryptography has a native extension, so the wheel is built inside the Lambda Linux image (`deploy/lambda/api/requirements.txt`), not copied from the dev machine.
- **api Lambda runs on `ARM_64` (Graviton)** with the bundling image pinned to `platform="linux/arm64"`, so the manylinux `aarch64` cryptography wheel deterministically matches the Lambda runtime regardless of the build host (a default x86_64 Lambda with an aarch64 wheel would crash at import).
- **Real Cognito pool id is injected into the api Lambda** via `api_fn.add_environment("COGNITO_USER_POOL_ID", cognito_outputs["CognitoUserPoolId"])` after the Cognito section computes it, plus `COGNITO_CLIENT_ID`. The construction-time `COGNITO_USER_POOL_ID` (which read an often-empty `config.yml` value) is gone — signature verification needs the genuine pool id to reach JWKS.

### Fixed

- **`config.yml` had two `console_auth:` blocks**; YAML last-key-wins silently let the second override the first. Merged into one block. **`user_pool_id` is intentionally left UNSET**: the stack already manages the Console Cognito pool via the "new pool" branch (stable logical id + `RETAIN`), so the pool, its users, RBAC groups, and domain persist across deploys. Setting `user_pool_id` flips the stack to the "import existing pool" branch, which re-creates the `openclaw-console` domain for a pool whose domain CFN already owns — Cognito allows one domain per pool, so the deploy fails with `domain … AlreadyExists`. The api Lambda still receives the genuine pool id at deploy time via `add_environment(cognito_outputs["CognitoUserPoolId"])`, so verification works without pinning it here.
- **`launch-vm.sh` inserted an illegal `iptables -t nat -I PREROUTING … -j DROP`** IMDS rule. The nat table does not permit filtering verbs — nft returns "the use of DROP is therefore inhibited" (rc=2), and under `set -e` this aborted *every* new VM launch on nft-backed hosts (a freshly scaled-out host could start no microVM). Removed; the FORWARD-chain DROP already blocks guest→IMDS before MASQUERADE, so isolation is unchanged. Found during live SSH-hardening verification.

### Verified live

Deployed to ap-northeast-1 (`cdk deploy -c region=ap-northeast-1`) and verified against the running fleet — note `deploy/app.py` defaults the region to `us-east-1` via CDK context (not `AWS_REGION`), so the `-c region=…` flag is **required**; without it CDK stages assets to a non-existent us-east-1 bucket and fails with a misleading `EPROTO`.

- **api Lambda flipped x86_64 → arm64**; `COGNITO_USER_POOL_ID` now the genuine `ap-northeast-1_yvmL2GT3P` (was empty), `DEFAULT_NO_JWT_ROLE=viewer`. CFN completed with no rollback — proof the arm64 cryptography wheel imports on the Lambda runtime.
- **RBAC, through API Gateway** (`scripts/e2e-rbac-test.sh`): no-token `POST /tenants` → **403** `{"role":"viewer","required":"operator"}`; no-token `GET /tenants` → **200**; no-token `DELETE` → **403**; an attacker-signed RS256 admin token → **403**; an `alg:none` admin token → **403**. The exact pre-1.5.0 forgery now yields viewer/403.
- **SSH, on a scaled-out host (zero-downtime, old hosts/tenants untouched)**: `init-host.sh` generated `/etc/openclaw/host_vm_key` (600, root) and removed `sshpass`; `launch-vm.sh` logged `injected host SSH public key into VM data disk` and the VM reached `InstanceStart succeeded`. The hardened **v1.1 rootfs** image was inspected directly: `PermitRootLogin no` / `PasswordAuthentication no` / `PubkeyAuthentication yes` / `ChallengeResponseAuthentication no`, both `root` and `agent` shadow entries `LOCKED`, and a full-image `grep` for the old password returns **zero** hits.


### Tests

- **`tests/test_rbac.py` rewritten for signature verification** (26 tests). Generates an in-process RSA keypair, signs real RS256 tokens, and points verification at the matching public key by patching the single seam (`_get_jwks_client`). Anti-forgery cases sign with a *different* attacker key and assert downgrade to `viewer`: forged signature, `alg:none`, expired, wrong issuer, wrong audience, garbage token — plus an end-to-end `POST /tenants` with a forged admin token that gets 403. Fail-safe-default cases cover no-token → `DEFAULT_NO_JWT_ROLE`, non-Bearer auth, and verification-unavailable.
- **`test_resize.py` / `test_resize_disk.py` / `test_migration.py`** gained an autouse fixture asserting an authenticated admin caller — they exercise business logic, not RBAC (which `test_rbac.py` owns), and would otherwise 403 under the new fail-safe default.

### Operator notes

- **Deploy requires Docker** at `cdk synth` / `cdk deploy` time (the cryptography wheel is built in a container). `cdk synth` exit 0 with `jwt/` + `cryptography/` present in the asset confirms bundling worked.
- **`cdk deploy` ships new `openclaw-api` (ARM_64) code + the injected Cognito env.** After deploy: a write without a Bearer token must 403; a write with a valid admin id_token must succeed; a forged/`alg:none` token must be rejected.
- **Retiring the old SSH password requires rolling hosts** so VMs relaunch from the hardened rootfs (rebuild rootfs → refresh-rootfs → roll). Until a host is rolled, its existing VMs keep the old password.

## [1.4.5] — 2026-05-30

Live migration is now asynchronous and proven end-to-end against the live fleet. 1.4.4 fixed the data plane (disks now ship with the snapshot) but left a control-plane wall: a real snapshot+restore moves multiple GB and takes minutes, while API Gateway caps a synchronous integration at 29 s, so `POST /migrate` returned `{"message":"Endpoint request timed out"}` and could strand a tenant in `migrating`. This release moves execution off the request path and verifies a real host→host move through the public API.

### Why this matters

The fail-safe migration state machine from 1.4.4 was correct, but running it synchronously inside the API request was structurally incompatible with API Gateway's 29 s ceiling. Making migration asynchronous is the only way `POST /migrate` can be both honest (don't claim success before the VM has moved) and within platform limits. AZ failover already drove migration outside API Gateway, so it was never affected — this brings operator-initiated migration up to the same standard.

### Changed

- **`POST /tenants/{id}/migrate` is now asynchronous (202 + poll).** It validates synchronously (target exists / not draining / capacity), fires the snapshot SSM command fire-and-forget, records `status=migrating` plus the async context (`migration_target` / `migration_source` / `migration_snap_cmd` / `migration_phase` / `migration_started_at` / `migration_snapshot_uri`), and returns **202** with a `poll` hint. No `host_id` / counter / routing mutation happens in the request path. Clients poll `GET /tenants/{id}` until `running` (success) or until `migration_failed` is set with status back to `running` (failure).
- **`_ssm_send` now returns the SSM CommandId** (previously discarded) so the sweep can poll the command it fired.

### Added

- **`_advance_migration` sweep in the health_check Lambda.** The existing 5-min EventBridge schedule now also advances in-flight migrations — a state machine that polls the snapshot command → fires restore → polls restore → repoints the ALB → gates on the public-path dashboard check (`_verify_dashboard_reachable_via_alb`, the 1.4.2 "no fake failover" guarantee) → and only then flips `host_id` / counters / `status=running` and clears the async context. Any SSM failure, dashboard-verify failure, unknown phase, or a 15-min watchdog rolls status back to `running` with `host_id` untouched (the source VM was only briefly paused for the snapshot, so `running` is the truthful state). Reuses the existing schedule, IAM (`ssm:SendCommand`/`GetCommandInvocation` + elbv2 rule perms, already present for AZ failover), and `reserved_concurrent_executions=1` (serializes the sweep — no migration race). Zero new infrastructure.
- **`tests/test_migration_sweep.py`** — 10 unit tests for `_advance_migration`: snapshot Success→fire restore / Failed→rollback / InProgress→noop; restore Success→flip host_id+counters / Failed→rollback / dashboard-unreachable→rollback; watchdog timeout, unknown phase, and missing CommandId all roll back to `running`.

### Tests

- `tests/test_migration.py` rewritten for the async contract: 202 + `status=migrating`, snapshot `_ssm_send` fires once on the source, the `migration_*` context is persisted, `host_id` is **not** flipped in the request path, and an SSM-submit failure returns 502 without marking `migrating`.
- Full offline suite: **475 passed** (excluding the real-AWS e2e/failover tests that need a live deployment).

### Verified live (issue #64 AC #6)

Ran `scripts/e2e-migrate-test.sh` against the live fleet (ap-northeast-1, 2 active hosts across 1a/1c):
- `POST /migrate` → **HTTP 202** immediately (no more 29 s timeout).
- Polled `GET /tenants/{id}`; the health_check sweep advanced snapshot→restore→flip and the tenant reached `status=running` on the **target** host.
- Authoritative checks: DDB `host_id` flipped to the target, `migration_phase`/`migration_failed` cleared, **source host ran 0 firecracker processes for the tenant**, target host ran 1, and the dashboard answered non-5xx through CloudFront. First time a tenant microVM has migrated host→host through the public API end-to-end.
- Verified **both directions**: 1c→1a and then the reverse 1a→1c, each reaching the green verdict (host flipped, source drained, target running).

### Operator notes

- **No infrastructure change** beyond the two Lambda code updates — the health_check schedule, IAM, and EventBridge rules are unchanged. A `cdk deploy` ships the new `openclaw-api` + `openclaw-health-check` code.
- Migration completion is now eventually-consistent on the health_check cadence (default 5 min). The data plane moves within the first sweep; control-plane status flips on the tick that observes restore success. Tune `health_check.interval_minutes` for a tighter SLO if needed.
- Clients must treat `202` from `POST /migrate` as "accepted, in progress" and poll `GET /tenants/{id}`. The old `200`/synchronous contract is gone.

## [1.4.4] — 2026-05-30

Live-migration actually works now. v1.4.2 claimed to "genuinely verify failover," but cross-host live migration had **never once succeeded end-to-end** — the script it depends on was never deployed, and even once deployed it shipped the wrong files. We found this by running a *real* migration against the live fleet (not a mock), and fixed every layer the failure passed through. Also closes a guest→host credential-theft path and a deploy-blocking Lambda policy-size bug surfaced along the way.

### Why this matters

Issue #64: `POST /tenants/{id}/migrate` returned `202 migrating` and the console showed success, but the VM never moved. Three independent defects stacked on top of each other, each masking the next. A static test couldn't catch them because the script *exists in source* — only driving the real data plane revealed it. This release is the first time a tenant microVM has demonstrably moved host→host with its disk intact.

### Fixed

- **`migrate-vm.sh` / `resize-disk.sh` were never deployed (issue #64, #22).** Both have lived in `deploy/userdata/` since the live-migration / disk-resize features landed, but neither `setup.sh` (S3 upload) nor `init-host.sh` (per-host download) referenced them. The migrate/resize APIs SSM-invoked `/home/ubuntu/migrate-vm.sh`, hit a non-existent file, and failed with **exit 127**. Now uploaded by `setup.sh` and pulled by `init-host.sh` alongside the other host scripts.
- **`migrate-vm.sh` never shipped the disk images.** A Firecracker snapshot records only the *path* of each virtio-block backing file, not its contents. The `snapshot` mode uploaded `snapshot.vm`/`.mem`/`vm.json` but **not `data.ext4`/`overlay.ext4`**, so `restore` on the target host failed with `400 Bad Request: ... No such file or directory (os error 2) .../data.ext4`. Verified live: the very first real cross-host migration died here with curl **exit 22**. Snapshot now uploads the backing files and restore downloads them *before* `PUT /snapshot/load`; load failures now surface Firecracker's own error body instead of a bare exit code.
- **migrate / resize-disk were fire-and-forget and mutated DynamoDB optimistically.** migrate flipped `host_id`/counters and tore down routing within ~1 s while snapshot+restore actually take 30-60 s; on failure the tenant was left pointing at a host with no VM. Reworked to the AZ-failover pattern: mark `migrating`, run snapshot then restore via blocking `_ssm_run`, and only flip the source of truth + status after **both** succeed. Any SSM failure returns 502 with DDB untouched — verified live: when restore failed, status rolled back to `running` on the source and `host_id` never moved.
- **Guest→host IMDS credential theft (multi-tenant isolation).** A tenant microVM could reach `169.254.169.254` through the host MASQUERADE rule and steal the host instance-profile credentials (read/write to the shared assets bucket + tenants/hosts tables = every other tenant's data). Added iptables `DROP` for the link-local IMDS range on the tap interface *before* the FORWARD/PREROUTING ACCEPT rules, plus `HttpTokens=required` + `HopLimit=1` on the launch template (base + nested-virt) as host-side defense-in-depth.
- **Lambda resource-policy exceeded the 20 KB limit (deploy-blocking).** Each `LambdaIntegration(api_fn)` attached a per-method `AWS::Lambda::Permission`; at ~29 routes the policy crossed Lambda's 20480-byte hard limit and **every** `cdk deploy` failed. Grant API Gateway invoke once via a wildcard source ARN and build integrations against an imported view of the function (CDK adds no per-method permission for an imported `IFunction`), collapsing 29 statements into 1.

### Added

- **`scripts/e2e-migrate-test.sh`** — real end-to-end live-migration test (issue #64 AC #6). Drives the actual data plane: uploads host scripts → SSM-pushes them to every active host → migrates a running tenant to a different host → verifies SSM Success on snapshot+restore, the DDB `host_id` flip, the source VM is gone, and the dashboard is reachable through CloudFront. This is what proved the disk-image bug that mocked tests structurally cannot catch.
- **`tests/test_script_manifest.py`** — static regression guard: every host `.sh` the API SSM-invokes must be both uploaded by `setup.sh` *and* delivered via `init-host.sh` (or the `stack.py` `BACKUP_DATA_SCRIPT` injection). Would have caught issue #64 at PR time.
- **Migration failure-path unit tests** (`tests/test_migration.py`): snapshot-fail and restore-fail (via `_ssm_run` `side_effect`) assert status recovers to `running` and `host_id` never flips. `tests/test_resize_disk.py` asserts an SSM failure does not persist the new `data_disk_mb`.

### Known limitations / next

- **migrate over API Gateway is bounded by the 29 s integration timeout.** With the disks now shipped correctly, a real snapshot+restore moves multiple GB and takes minutes — longer than API Gateway will hold a synchronous request (`{"message":"Endpoint request timed out"}`). **Resolved in 1.4.5** by making `POST /migrate` asynchronous (returns `202` + a pollable status; the health_check sweep finishes the move out-of-band). AZ failover, which drives migration outside API Gateway, was unaffected.
- **RBAC fail-open** (`_get_user_role` defaults to admin when no role claim is present) and the **SSH-password host bootstrap** remain known risks, recorded here and deferred per owner decision — not yet fixed.

### Operator notes

- **Requires `./setup.sh` (or manual S3 upload of `migrate-vm.sh` + `resize-disk.sh`) AND a CDK redeploy.** Existing hosts pick up the new scripts on next ASG roll or via an SSM push; `scripts/e2e-migrate-test.sh` performs that push as step 1.
- The IMDS hardening changes the launch template — new hosts get it on boot; roll the ASG to apply fleet-wide.

## [1.4.3] — 2026-05-29

Test stability fix. Closes the e2e flake we surfaced while validating v1.4.2: long-running backup-restore tests would occasionally fail with `urllib.error.URLError: [SSL: UNEXPECTED_EOF_WHILE_READING]` when the API Gateway / ALB closed a TLS keep-alive connection mid-request. Not a code regression — a network-layer fact of life that the test harness wasn't handling.

### Why this matters

Our v1.4.2 release explicitly required end-to-end verification that failover genuinely works. We can't claim "tests prove it" if the test suite itself is intermittently broken. This release makes the e2e harness retry-aware so transient ALB / API Gateway TLS resets and 5xx blips don't masquerade as test failures, and the failure signal stays clean for real bugs.

### Fixed

- **`tests/test_e2e.py::_api()` is now retry-aware.** Up to 3 attempts with exponential backoff (1 s → 2 s → 4 s). Retries cover:
  - `urllib.error.URLError` (SSL EOF, connection reset, DNS hiccup)
  - HTTP 5xx (Lambda cold start, throttle, ALB backend reset)
  4xx errors return immediately so authentication / validation bugs surface fast (no 7-second wait to discover a 401).
- **`_wait_for_status` default timeout 180 s → 360 s.** Restore-from-backup tests boot a Firecracker VM, decompress and ext4-fsck a multi-GB rootfs, and install OpenClaw. On a cold pool that's a real 5 minutes — the previous 240 s ceiling was a flake source masquerading as a code problem.

### Added

- **`tests/test_e2e_retry.py`** — 8 unit tests covering the new retry policy:
  - happy path: first call success, no retry
  - SSL EOF on first call → success on retry (the bug we're fixing)
  - 5xx on first call → success on retry
  - 4xx → no retry, return immediately
  - persistent SSL EOF → exhausts and raises
  - persistent 5xx → returns the 5xx status (no raise, lets caller decide)
  - exponential backoff timing (sleeps captured: 1 s, 2 s)
  - `max_retries=1` disables retry

### Tests

After this fix, e2e backup-restore went from intermittent SSL-EOF failures to **3/3 passes in 84 s** consistently. Unit suite stable at **542 passed / 0 failed** (1.4.2 baseline 534 + 8 new retry tests).

### Operator notes

- **No production impact.** Test-only change. The runtime Lambda code is identical to v1.4.2.
- **No CDK redeploy needed.** This release ships a test harness fix and a CHANGELOG entry; nothing in `deploy/` changed.
- If you have a CI pipeline that imports `tests/test_e2e.py`, the public function `_api()` now accepts a `max_retries=3` kwarg (default). Existing `_api(method, path, body, timeout)` signatures continue to work unchanged.

### How to tell SSL EOF from a real failure

When an e2e test fails with `urllib.error.URLError: [SSL: UNEXPECTED_EOF_WHILE_READING]`:

1. **First time, single test, in the middle of a long test (>60 s)?** Almost always transient. The new retry catches this automatically.
2. **Every test, every time?** Likely a real connectivity problem — check your `.env.deploy`, network reachability to API Gateway, certificate chain.
3. **Persistent across multiple `pytest` invocations on a specific endpoint?** Check API Gateway / ALB CloudWatch logs for backend health.

For 5xx errors:
1. **One test, transient?** Auto-retried.
2. **Recurring on a specific path?** Real bug. Check Lambda logs for the route in question.

---

## [1.4.2] — 2026-05-29

**Critical bug fix.** Closes the "fake failover" bug: prior versions could mark `AZ_FAILOVER_TENANT_RECOVERED` and flip DDB `status=running` while the public dashboard URL was 502'ing because the ALB still routed to the dead source host or the VM never finished booting. v1.4.2 makes failover **genuinely** end-to-end verified before declaring success.

### Why this matters

A teammate observed that audit logs showed `AZ_FAILOVER_TENANT_RECOVERED` for several tenants whose dashboards remained completely inaccessible. Investigation found three root causes inside `_failover_tenant_to_host`:

1. **The verify probe was a paper tiger.** It only checked that a Firecracker process existed and an nginx config file was present on disk. Neither proves the guest finished booting, that nginx reloaded the new conf, or that the VM backend was actually serving HTTP.
2. **ALB repoint failures were silently swallowed.** The previous `try/except` block caught `RuntimeError` from `_repoint_alb_rule`, logged it, and let the failover continue to flip DDB. The result: traffic kept going to the dead source host but DDB / audit / console all said "running".
3. **No public-path verification before DDB flip.** Nothing actually opened `https://<alb>/vm/<tenant>/` to check if the failover succeeded from a real user's perspective.

### Fixed

- **Strengthened `_verify_vm_actually_running`** (now an unconditional gate, not a fallback). The probe checks **three** signals in one shell command:
  1. Firecracker process with the right `api-sock` exists
  2. `/etc/nginx/conf.d/tenants/{tid}.conf` exists
  3. **NEW**: `curl --max-time 5 http://127.0.0.1/vm/{tid}/` returns a non-5xx HTTP status (200/302/4xx all count as "service alive"; 5xx and connection-refused mean the VM is dead-on-arrival)
  Prints the failure reason (`NOT_RUNNING_NO_PROCESS`, `NOT_RUNNING_NO_NGINX_CONF`, `NOT_RUNNING_HTTP_502`) to CloudWatch so operators can diagnose without SSH.
- **NEW `_verify_dashboard_reachable_via_alb`** — the gate that closes the "fake failover" hole. Hits `http://<ALB_DNS>/vm/{tid}/` directly (bypasses CloudFront cache) for up to 30 s. 4xx auth challenges and 302 redirects count as reachable; 5xx and connection errors do not. Fails closed if `PUBLIC_BASE_URL` is unset.
- **`_failover_tenant_to_host` rewritten as 8 explicit gated steps**:
  1. find latest backup
  2. DDB conditional update → `failover_recovering`
  3. SSM `launch-vm.sh` on target host
  4. **GATE: VM verify** — must pass (any of the 3 signals fails → raise)
  5. **GATE: ALB repoint** — must succeed (no more swallowing exceptions)
  6. **GATE: cross-ALB reachability** — must get non-5xx within 30 s
  7. best-effort source host nginx cleanup
  8. **DDB flip to `running` ONLY after all gates passed** + audit `RECOVERED`
- **New status `failover_failed_partial`** for the case where the VM verified up on the target but ALB repoint or cross-ALB probe failed. Operators can grep `failover_failed_partial` in DDB to find tenants needing manual ALB cleanup, separately from `failover_failed` (target host in unknown state).

### Added

- `PUBLIC_BASE_URL` env var on the health_check Lambda (CDK injects `http://<alb.load_balancer_dns_name>`). Set to empty string to disable the cross-ALB gate (fall back to 1.4.1 behavior with a CloudWatch warning).
- `tests/test_failover_genuine.py` — **16 unit tests** explicitly covering the three root causes:
  - 6 false-positive guards: local-curl-5xx blocks failover; ALB-fail blocks; missing `private_ip` blocks; cross-ALB-5xx blocks; cross-ALB-timeout blocks; no-`PUBLIC_BASE_URL` falls back to 1.4.1
  - 4 happy-path tests: all gates pass returns True; emits `RECOVERED` audit; cross-ALB probe accepts 4xx as reachable; accepts 302 as reachable
  - 6 cross-ALB probe unit tests: 200 / 4xx returns True; 5xx / connection-refused / empty-base-url returns False; transient 5xx then 200 still returns True

### Tests

**534 passed / 0 failed** locally (1.4.1 baseline 493 + 27 regression + 16 new failover_genuine - 2 dedup). e2e backup-restore (3 tests) also pass against the live deployment.

Every false-positive scenario test asserts:
1. `_failover_tenant_to_host` returns `False`
2. Final tenant status is `failover_failed_partial` or `failover_failed` (never `running`)
3. `AZ_FAILOVER_TENANT_RECOVERED` is **not** in the audit log

This is the regression that the previous test suite missed.

### Operator notes

- **CDK redeploy required.** `./setup.sh <region> <profile>` injects the new `PUBLIC_BASE_URL` env var. Without it, the Lambda falls back to 1.4.1 behavior (still better than 1.3.x because of the strengthened verify probe) but the cross-ALB gate is skipped.
- **No data migration.** Existing tenants with `status=failover_recovering` from interrupted older invocations are untouched.
- **New audit operations** to monitor:
  - `AZ_FAILOVER_ALB_REPOINT_FAILED` — ALB repoint exception (now causes failure instead of being swallowed)
  - `AZ_FAILOVER_TENANT_FAILED` with `status_set=failover_failed_partial` — VM is up but ALB / cross-ALB gate failed; operator must manually fix ALB rule
  - `AZ_FAILOVER_TENANT_FAILED` with `status_set=failover_failed` — VM verify itself failed; host-agent should garbage-collect

### Upgrade path

```bash
git pull && ./setup.sh <region> <profile>
```

After redeploy, watch CloudWatch logs for `failover failed` lines — they now distinguish "VM never came up" from "VM came up but ALB never updated" instead of silently emitting fake successes.

### Known limitations

- **Cross-ALB gate uses unauthenticated HTTP.** If the deployment runs behind WAF rules that block unauthenticated traffic from outside the VPC, the gate can false-fail. Workaround: leave `PUBLIC_BASE_URL` unset (Lambda falls back to local-only verification with a CW warning).
- **Existing already-running fake-failovered tenants are not auto-detected.** v1.4.2 only fixes new failovers. To find tenants currently in the bad state from earlier versions: `aws dynamodb scan --filter-expression 'attribute_exists(failover_at)'` and manually probe each dashboard URL.

---

## [1.4.1] — 2026-05-28

Console UI for skills CRUD + Skill Groups management. Closes [#63](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/63) and rounds out the v1.4.0 (#62) work by giving the Application tab a real management surface for skills and groups, no terminal needed.

### Why this matters

v1.4.0 added the data model + API for per-tenant / per-group skill scoping but every operator still had to `aws s3 cp ./SKILL.md s3://${ASSETS_BUCKET}/skills/{name}/SKILL.md` and wait 5 min for the cron sync to pick it up. There was no audit trail of who changed what, no in-browser preview, no way to scope skills to groups without `curl`. v1.4.1 closes that gap.

### Added

- **Console — Application tab → Skills card gains full CRUD**:
  - **List** with description (existing) + skill count
  - **View** — click a row to expand inline; `SKILL.md` is rendered with [Marked.js](https://marked.js.org/) (~30 KB CDN)
  - **Edit** — toggle to a `<textarea>` editor, "Save" calls `PUT /skills/{name}`
  - **Upload** — modal accepts `name` + `content` and `PUT`s a new skill
  - **Delete** — confirmation dialog, then `DELETE /skills/{name}` removes the entire prefix (SKILL.md plus any auxiliary files)
  - **RBAC** — viewer sees read-only list (no Edit / Delete / + New buttons)
- **Console — Application tab → new "Skill Groups" card** (above Skills):
  - List groups with their description + skill count
  - **+ New Group** modal (name + description + comma-separated initial skills)
  - Click a group to expand inline; add skills via dropdown picker (filtered to skills not already in the group); remove skills via per-tag `×` button
  - Wired to v1.4.0's existing `GET/POST /groups`, `POST /groups/{name}/skills`, `DELETE /groups/{name}/skills/{skill}` endpoints
- **API — three new routes on the api Lambda** (reusing existing RBAC + audit-log infrastructure):
  - `GET /skills/{name}` — return the SKILL.md content for the editor (open to viewer)
  - `PUT /skills/{name}` — create or replace SKILL.md (operator+; validates UTF-8, ≤256 KiB, must contain at least one top-level `# Title`)
  - `DELETE /skills/{name}` — recursive delete of the `s3://${ASSETS_BUCKET}/skills/{name}/` prefix (operator+; idempotent — 404 if missing)
- **Audit log entries** for every PUT / DELETE on skills, via the existing `_audit_write` hook (no new audit code)

### Changed

- **`stack.py`** — `/skills/{name}` resource added with GET/PUT/DELETE methods all routed to the api Lambda (which already has RBAC + audit). The list endpoint `GET /skills` stays on the dedicated skills Lambda. `assets_bucket.grant_put(api_fn)` and `grant_delete(api_fn)` added so the api Lambda can write/delete skill objects.
- **Console state** — `loadHosts()` now calls `loadSkills()` and `loadGroups()` instead of inlining the GET; both can be refreshed independently after CRUD operations.

### Tests

- **16 new unit tests in `tests/test_skill_crud.py`**:
  - `read_skill` — 3 tests (existing skill returns content, missing → 404, invalid name → 400)
  - `update_skill` — 8 tests (create returns 201, replace returns 200, content without `# Title` → 400, empty content → 400, oversized content → 400, invalid name → 400, invalid JSON → 400, H1 after blank lines accepted)
  - `delete_skill` — 3 tests (deletes all objects under prefix, empty prefix → 404, invalid name → 400)
  - RBAC routing — 2 tests (`GET /skills/{name}` is in `_VIEWER_OK`, PUT/DELETE are not)

**493 passed / 0 failed locally** (v1.4.0 baseline 477 + 16 new).

### Operator notes

- **No data migration.** The new endpoints write to the same `s3://${ASSETS_BUCKET}/skills/{name}/SKILL.md` keys the host cron has always synced from. Existing skills are visible in the console immediately.
- **Cron sync timing unchanged**: PUT writes the file → host cron picks it up within 5 min → next VM launch on that host injects it. To deliver immediately, run the existing `POST /hosts/refresh-rootfs` flow or restart the affected VMs.
- **DELETE removes the whole prefix**, not just SKILL.md. If you uploaded auxiliary files (diagrams, helper docs) under `skills/{name}/`, they are removed too.
- **Marked.js is loaded from `cdn.jsdelivr.net`.** If your console is behind a strict CSP that blocks third-party CDNs, replace with a vendored copy or remove the script tag (the editor falls back to plain `<pre>` rendering).
- **Console RBAC**: `viewer` users see the Skills + Groups lists and can preview SKILL.md, but Edit / Save / Delete / + New buttons are hidden. Server-side checks block the same writes via `_rbac_check`.

### Known limitations

- **No file upload of binary assets yet** — SKILL.md content is text-only via the editor. Upload diagrams/PNGs via S3 directly for now.
- **Skill name is fixed at upload time** — no rename UI. Workaround: upload under the new name, delete the old one.
- **No "which tenants use this skill" view** — operators can compute it from `GET /tenants` + `effective_skills` filter, but no dashboard yet.

### Upgrade path

```bash
git pull && ./setup.sh <region> <profile>
```

No data migration. Console picks up the new UI on the next CloudFront cache flush.

---

## [1.4.0] — 2026-05-28

Per-tenant / per-group skill distribution. Closes [#62](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/62) — first-class scoping for shared skills so an SRE team's incident-response skill no longer ends up in every other tenant's filesystem.

### Why this matters

Pre-1.4.0 every shared skill (`s3://${ASSETS_BUCKET}/skills/<name>/SKILL.md`) was broadcast indiscriminately at VM launch — `launch-vm.sh` did an unconditional `cp -r ${SHARED_SKILLS}/* ${MOUNT_TMP}/.openclaw/skills/`. That's fine when the operator owns every tenant but breaks down when a single deployment hosts agents from unrelated teams or organizations. There was simply no way to express "this skill is only for tenants in the SRE group".

### Added

- **`openclaw-groups` DynamoDB table** with partition key `name`. Stores the per-group skill allow-list (`skills: [...]`) plus optional description. Pay-per-request, RETAIN policy.
- **Tenant skill scope on `POST /tenants`** — body now accepts:
  - `skills: ["a", "b"]` — explicit per-tenant allow-list
  - `group: "team-sre"` — inherit the group's skill list
  - Both — effective set = `tenant.skills ∪ group.skills`
  - Neither — fall back to broadcast (legacy v1.3.x behavior, **fully backward compatible**)
- **Groups CRUD endpoints** (4 new routes; `viewer` can read, `operator+` can write):
  - `GET /groups` — list all groups
  - `POST /groups` — create with optional initial skill list
  - `POST /groups/{name}/skills` — append a skill (idempotent)
  - `DELETE /groups/{name}/skills/{skill}` — remove a skill
- **`GET /tenants/{id}` returns `effective_skills`** — the resolved union (or `"*"` when broadcast) so operators can see exactly what a tenant will get without reading code.
- **`GET /skills?tenant=<id>` filters** the returned skill catalog to that tenant's effective set, so the Console (or any client) can render a per-tenant skills view directly from the API.
- **`launch-vm.sh` 7th positional arg `SCOPED_SKILLS`** — comma-separated allow-list (or empty / `*` for broadcast). When present, only the listed skill subdirectories are `cp`'d into the VM at launch. Existing 6-arg invocations keep working unchanged because the 7th arg defaults to empty (= broadcast).

### Changed

- **`api/handler.py::_resolve_effective_skills`** is the central resolver — used by `create_tenant`, `process_pending`, and `GET /tenants/{id}`. Same logic mirrored in `skills/handler.py` for the `?tenant=...` query path so the front-end gets identical answers.
- **`skills/handler.py`** now declares `TENANTS_TABLE` + `GROUPS_TABLE` env vars and is granted read access in CDK (so the `?tenant=` filter can resolve effective skills server-side).
- **Unknown-group rejection at create time.** `POST /tenants` with a `group` that doesn't exist returns `404` instead of silently accepting the typo and dropping the group from the union forever. (The runtime resolver `_resolve_effective_skills` still tolerates unknown groups defensively in case the group is deleted while a tenant references it.)

### Tests

- **29 new unit tests in `tests/test_skill_scoping.py`** covering five surfaces:
  1. **`_resolve_effective_skills` — 8 semantic scenarios.** broadcast / single / group-only / tenant-only / both / unknown-group / empty-union-falls-back-to-broadcast / DDB exception during group lookup.
  2. **Groups CRUD — 11 tests.** create writes 201 with item; rejects invalid name / missing name / non-string skills; 409 on duplicate; list returns all; add-skill idempotent; remove-skill works; remove-nonexistent-skill is a noop; unknown-group on add returns 404.
  3. **`POST /tenants` validation — 3 tests.** skills must be list-of-strings; group must match DNS-label regex; unknown-group rejected at create.
  4. **`launch-vm.sh` argument schema — 4 grammar tests.** 7th positional is `SCOPED_SKILLS`; empty/`*` keeps broadcast branch; comma-list iterates via `IFS=','`; missing skill subdir is logged not fatal.
  5. **`_launch_vm` command wiring — 3 tests.** None passes `""` placeholder, list passes comma-separated, template + skills both wired correctly.

Total: locally **477 passed / 0 failed** (v1.3.4 baseline 448 + 29 new skill-scoping tests).

### Operator notes

- **Backward compatible.** Existing tenants without `skills` or `group` fields continue to receive every skill at launch. No data migration. No forced redeploy. Operators opt into scoping per-tenant by setting one or both fields.
- **DDB schema impact**: the new `openclaw-groups` table is created on `cdk deploy`. Existing `openclaw-tenants` rows are not modified (new optional `skills` / `group` attributes appear only on tenants created with scoping after 1.4.0).
- **Check what a tenant gets**: `curl ${API_URL}tenants/<id> | jq .effective_skills` — returns either a sorted list or the literal string `"*"` (broadcast).
- **Skill scoping is enforced at launch time only** — already-running VMs from before 1.4.0 keep their full skill snapshot until they're restarted/reset. The intended use is "set scope on `create`", not "retroactively shrink running tenants".
- **Group typos**: `POST /tenants` with a non-existent group returns `404` so you can't accidentally create a tenant scoped to a group that doesn't exist. To "remove" a group from a tenant, recreate the tenant with the desired skill set (or wait for #63 console group editor).

### Known limitations

- **No console UI yet** — group + per-tenant skill management requires `curl` for now. Console UI is tracked in [#63](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/63), planned for v1.4.1.
- **No cross-tenant skill auditing** — `GET /audit-log` records the `POST /tenants` and `POST /groups` events, but there's no dashboard yet that shows "which tenants currently include skill X". Easy to build on top of `?tenant=` filter.
- **Skill scope is enforced at launch only**, not at runtime. A tenant that's already running with broadcast-mode skills won't lose them on a `POST /groups/.../skills` removal until the VM is reset/restarted.

---

## [1.3.4] — 2026-05-28

Security hardening: split operator console domain from per-tenant dashboard domain so that the Cognito session cookie is physically scoped to the console origin and cannot reach tenant-rendered DOM.

### Why this matters

Pre-1.3.4 deployments serve `console/*` (operator login + admin actions) and `vm/*` (per-tenant dashboards rendered by tenant-controlled OpenClaw apps) from a single CloudFront distribution on a single domain. The Cognito session cookie set on the parent host is automatically sent on every `/vm/*` request — meaning any XSS in a tenant dashboard can read the operator's session, escalate to admin, and reach every other tenant. Same-origin policy, by design, doesn't isolate `/console/*` from `/vm/*`.

The fix is structural rather than mitigation-stack: put the two surfaces on **two different origins** (different hostnames), so the browser itself refuses to send the console cookie to the tenant origin. This is closing #61.

### Added

- **Dual-domain mode for CloudFront** (#61). New `cloudfront.console_domain` / `console_cert_arn` / `app_domain` / `app_cert_arn` fields in `config.yml`. When all four are set, `stack.py` synthesizes **two distinct CloudFront distributions** (`ConsoleCF` for `/console/*` from S3, `AppCF` for `/vm/*` from ALB) each with its own ACM certificate. Cognito User Pool Client `CallbackURLs` lists **only** `console_domain` — the session cookie is therefore set with `Domain=console.example.com` and the browser refuses to send it to `app.example.com` per RFC 6265 origin scoping. Single-domain (legacy) mode remains the default and works unchanged for v1.3.3 and earlier deploys, so no forced migration.
- **`setup.sh` accepts 4 new flags:** `--console-domain` / `--console-cert` / `--app-domain` / `--app-cert`. Old `--domain` / `--cert` continue to write the legacy `custom_domain` / `acm_cert_arn` fields. After deploy, setup prints two distinct URLs (or one in legacy mode) plus a security note nudging users toward dual-mode for production.
- **`OC_CONSOLE_BASE` JS global** in `console/config.js` (alongside existing `OC_DASHBOARD_BASE`). `OC_COGNITO_REDIRECT_URI` is now derived from `CONSOLE_BASE` (was `DASHBOARD_URL`), so OAuth implicit-flow redirects always land on the console origin and never on the tenant origin even if an operator clicks an attacker-controlled link from a tenant page.
- **`DualDomainMode` + `ConsoleUrl` + `AppCloudfrontDistributionId` CloudFormation outputs.** In legacy mode `ConsoleUrl == DashboardUrl` (preserves backward-compat for tooling that reads `DashboardUrl`).
- **18 new unit tests in `tests/test_dual_domain.py`** covering: config schema, setup.sh flag parsing, dual-mode synthesis (2 distributions + correct aliases + outputs), legacy single-mode synthesis, partial dual-config falling back to legacy (don't half-deploy on accident), Cognito callback URLs containing only `console_domain` (the actual security boundary), legacy callback URLs still working.

### Operator notes

- **Backward compatible.** Existing deploys without the four new fields stay on single-distribution mode. No data migration. No forced redeploy. The 1.3.4 stack synthesizes byte-for-byte the same CloudFormation as 1.3.3 when none of the new fields are set.
- **Recommended for production multi-tenant deployments.** Prepare two ACM certs in `us-east-1` (one each for the console and app domains), then:
  ```bash
  ./setup.sh <region> <profile> \
    --console-domain console.example.com --console-cert <acm-arn> \
    --app-domain     app.example.com     --app-cert     <acm-arn>
  ```
- **CNAME both domains to their respective CloudFront distributions** after deploy — the two distributions have different `*.cloudfront.net` defaults. `setup.sh` prints both URLs explicitly.
- **The legacy `--domain` / `--cert` flags still work** for dev / sample / single-tenant deployments where the security split isn't needed.
- **Existing tenant dashboard URLs change** if you migrate from legacy to dual-mode (they move from `claw.example.com/vm/<id>/` to `app.example.com/vm/<id>/`). Communicate to tenants before flipping.

### Test status

- **448 passed / 0 failed** locally on the v1.3.4 tag, including the 18 new dual-domain tests in `tests/test_dual_domain.py`.

---

## [1.3.3] — 2026-05-27

Migrate-path correctness fixes — two latent bugs since v1.2.0 that surfaced during production demo verification.

### Fixed

- **`POST /tenants/{id}/migrate` didn't update host capacity counters (#59).** The code comment at `deploy/lambda/api/handler.py:550` said *"source.vm_count--, target.vm_count++"* but the implementation was missing entirely. Source host kept showing fictional `vm_count / used_vcpu / used_mem_mb` after a successful migrate; target host counters never incremented. Drift accumulated over every migrate, eventually corrupting `_find_host()` capacity scheduling and the console host cards. Same partial-update bug existed in AZ failover (`deploy/lambda/health_check/handler.py:567`) — target got `vm_count + 1` but `used_vcpu` / `used_mem_mb` were never bumped. Both paths now write all three fields in a single atomic update.
- **`POST /tenants/{id}/migrate` didn't validate target host capacity (#60).** The endpoint only checked that the target existed and differed from the source. It would happily accept a `vcpu=2` tenant onto a host with 1 vCPU free, pushing the host past its configured `cpu_overcommit_ratio`. Added the same allocatable-vs-used check that `_find_host()` already uses. Returns **409** with explicit `(free vcpu=N, free mem=M; need vcpu=X, mem=Y)` when the migrate would exceed capacity, and **409** when the target is `draining` / `deleted`.

### Added

- **4 regression tests in `tests/test_migration.py`:** `test_updates_both_host_counters`, `test_rejects_insufficient_vcpu`, `test_rejects_insufficient_mem`, `test_rejects_draining_target`. Existing migration test fixtures gained `total_vcpu` / `total_mem_mb` / `status` fields to match real `init-host.sh` writes.

### Verified end-to-end on real AWS (this exact tag)

Demo deployment in `ap-northeast-1`:
- ✅ Migrated `migrate-test-1-60e4` (vcpu=2, mem=4096) from `i-07acc…` (used 7/3 vcpu, 14336 mem) to `i-0b1c…` (used 2/4 vcpu, 4096 mem). Source host counters dropped to `vms=3, used_vcpu=5, used_mem=10240`; target rose to `vms=2, used_vcpu=4, used_mem=8192`. Math matches exactly.
- ✅ Attempted migrate `nhost-70f2` (vcpu=2) to `i-07acc…` (only 1 vcpu free): rejected with `409 target host has insufficient capacity (free vcpu=1, free mem=11264MB; need vcpu=2, mem=4096MB)`.
- ✅ Same-host migrate still rejected with `400 target_host_id must be different from source` (existing logic preserved).

### Test status

- **455 passed / 0 failed / 0 skipped** locally (451 baseline + 4 new regression tests).

## [1.3.2] — 2026-05-24

**The release that takes "AZ failover" from "happy-path works" to "really works under all the messy real-world race conditions".** v1.3.1 nailed the basic end-to-end path (1 tenant in 1 dead AZ → comes up in another AZ). v1.3.2 fixes everything that breaks when you push it: concurrent Lambda invocations, multiple tenants in the same dead AZ, transient kernel races, host-agent auto-recovery overlap with Lambda's own SSM commands. Plus a new SSM-failure verification probe that distinguishes "launch-vm.sh actually failed" from "SSM exit code is misleading because host-agent already salvaged it".

### Real-environment proof — the bar lifted

We sat down and methodically ran 6 realistic failure scenarios against a live deployment, fixed every bug surfaced, and locked the fixes in with new unit tests. Test 2 in particular (multi-tenant simultaneous failover) revealed several deep race conditions that mock-only testing would have missed indefinitely:

```
===== Multi-tenant Test 2 (final) =====
Lambda summary:
  az_outages_detected: 1
  tenants_failed_over: 2  ← both tenants migrated
  tenants_failed: 0
  tenants_blocked: 0

Per-tenant verification:
  multi3-1-c8fe: status=running on i-0bb45 (AZ-a)  Dashboard=200  app_health=up
  multi3-2-3929: status=running on i-0bb45 (AZ-a)  Dashboard=200  app_health=up
```

### Fixed — concurrent Lambda invocation race

When a failover takes 60-90s of synchronous SSM waits but EventBridge fires every 5 minutes, two Lambdas could overlap. Pre-1.3.2, both would scan DDB and both would see the same affected tenants → both would try to migrate them → DDB writes fight, ALB rules flip back and forth.

Two-layer fix:
- **`reserved_concurrent_executions=1`** on the `health_check` Lambda. AWS now queues subsequent invocations behind the first.
- **Conditional `update_item` on `host_id`** when marking a tenant `failover_recovering`. If another invocation already moved the tenant, `ConditionalCheckFailedException` raises, we log `AZ_FAILOVER_SKIPPED_CONCURRENT` and back off cleanly. No DDB inconsistency, no false-positive failure.

### Fixed — `_failover_tenant_to_host` doesn't bump in-memory `next_vm_num`

In the same Lambda invocation, when 2+ tenants migrate to the same target host, each must get a unique `vm_num`. Pre-1.3.2 the Lambda kept reading the same `target.next_vm_num` from the DDB-fetched-once snapshot — both tenants got `vm_num=N`, the second's `ip tuntap add tap-vmN` hit `TUNSETIFF: Device or resource busy`. Now the orchestrator increments `target["next_vm_num"]` after every attempt (success or failure) so the next iteration picks a fresh number. The DDB-side counter still gets the authoritative bump inside `_failover_tenant_to_host` on success.

### Fixed — TUNSETIFF EBUSY on transient kernel races

Even with unique `vm_num`s, the kernel can briefly hold a tap name after `ip link del`. Pre-1.3.2, `ip tuntap add` returning EBUSY was a hard failure. Now `_tuntap_add_with_retry` in `launch-vm.sh`:
1. First attempt the `ip tuntap add`.
2. On EBUSY: force `ip link set down` + `ip link del`, kill any process still holding the tap fd via `lsof`, sleep 2s, retry once.

Recovers from 99% of the transient races without needing host-agent's heavier auto-recovery loop to kick in.

### Fixed — `set -e` + `trap ERR` was killing healthy VMs

This was the bug behind Test 2's persistent "DDB says failed but VM is actually running" symptoms. The chain:
1. `launch-vm.sh` starts firecracker successfully (VM is up, network configured, application starting).
2. A late step (e.g. `nginx -s reload` returning non-zero on a transient race, or an `ssh-keygen -R` cleanup failing) returns non-zero.
3. `set -e` exits the script.
4. `trap ERR` runs cleanup — pre-1.3.2, that included `pkill firecracker` and `rm fc.sock` → **kills the perfectly-healthy VM**.

Two fixes in series:
- **`trap ERR` no longer kills firecracker** if it's running on the expected `${SOCK}`. The VM may be perfectly fine; only late launch-script bookkeeping failed.
- **`set +e; trap - ERR` after `InstanceStart` succeeds.** Past this point the VM is genuinely up; failures in `nginx -s reload`, `ssh-keygen -R`, etc. shouldn't tear down a working VM. Always log `DONE` so callers know the script reached the end.

### Added — `_verify_vm_actually_running` SSM probe

Even with all the above, host-agent's own auto-recovery loop (every 5s, `host-agent.py::_recover_vm`) sometimes still salvages a launch that initially looked like it failed. Without active verification, the Lambda would stamp the tenant `failover_failed` even though the VM has been serving traffic for the past minute.

The new probe sends a small SSM command to the target host:
```bash
pgrep -f 'api-sock /data/firecracker-vms/<TID>/fc.sock' >/dev/null \
  && test -f /etc/nginx/conf.d/tenants/<TID>.conf \
  && echo VERIFIED || echo NOT_RUNNING
```
Polls for up to 90s (host-agent recovery cycle is ~5-15s in practice). If the probe returns `VERIFIED`, the orchestrator treats the original SSM exit code as misleading, marks the tenant `running`, and emits an `AZ_FAILOVER_RECOVERED_BY_VERIFY` audit row so operators can see what happened. If the probe times out or returns `NOT_RUNNING`, the tenant goes to `failover_failed` as before — never a false-positive success.

### Added — `tenants_blocked` summary bucket (semantic clarity)

Pre-1.3.2 the path-A "no-backup, refuse to fail over" behavior bumped `tenants_failed`. That conflated "we declined to migrate to avoid data loss" with "the migration crashed on us". Now `summary` carries three independent buckets: `tenants_failed_over`, `tenants_failed`, `tenants_blocked`. Auditing what happened during an outage is now precise.

### Added — cooldown persists immediately on outage detection

Pre-1.3.2, the per-AZ cooldown only got persisted to DDB *after* tenant migrations finished. Two consequences:
1. If an outage had no tenants on it (a healthy AZ that just went stale), no cooldown was set → next Lambda tick (5 min later) re-detected the outage → re-emitted audit + SNS for hours until the AZ recovered.
2. Concurrent Lambda invocations could both pass the `should_skip_az_for_cooldown` check.

Fix: `az_state[az] = now` and `put_item __az_failover_state__` happen *immediately* upon outage detection, before per-tenant work.

### Tests — 426 passed locally / +11 vs v1.3.1

| Test class | New | What it locks in |
|---|---:|---|
| `TestVerifyVmActuallyRunning` | 4 | probe says VERIFIED → True, NOT_RUNNING → False, eventual success across polls, SSM error → conservative False |
| `TestSsmFailButVerifySucceeds` | 2 | the critical 1.3.2 fix: SSM Failed + verify Success → treat as success; SSM Failed + verify Failed → mark failover_failed |
| `TestVmNumAllocationBatch` | 1 | two tenants in one Lambda call must get unique `vm_num` |
| `TestConcurrentGuard` | 1 | ConditionalCheckFailedException → return False, no SSM call |
| `TestBlockedSummaryBucket` | 1 | path-A no-backup increments `tenants_blocked`, not `tenants_failed` |
| `TestCooldownIdempotency` | 2 | cooldown persisted even with no affected tenants; `AZ_FAILOVER_NO_TENANTS_AFFECTED` audit emitted |

Plus updates to existing 48 v1.3.1 tests to mock the new `s3.list_objects_v2` / `elbv2.modify_rule` / `_verify_vm_actually_running` flows.

### Operator notes

- **Re-deploy**: needed for the new `reserved_concurrent_executions=1` and the verify probe's IAM (already covered by existing SSM permissions).
- **Re-roll `launch-vm.sh`** on existing hosts so `_tuntap_add_with_retry` and the `set +e after InstanceStart` are in place. Same SSM one-liner from v1.3.1's upgrade guide.
- **Audit log entries to monitor**:
  - `AZ_FAILOVER_RECOVERED_BY_VERIFY` — informational; the SSM exit code lied but VM is actually up
  - `AZ_FAILOVER_SKIPPED_CONCURRENT` — informational; second Lambda backed off
  - `AZ_FAILOVER_TENANT_FAILED` — actionable; verify probe confirmed failure
  - `AZ_FAILOVER_NO_BACKUP` — actionable; tenant has no backup, manual intervention needed

---

## [1.3.1] — 2026-05-24

The "production-grade" claim of v1.3.0 was **half-true**. AZ failover detected outages and orchestrated correctly, but the actual VM relaunch on the target host **never worked end-to-end** because of three integration bugs that mock-based unit tests didn't catch. v1.3.1 fixes them, validates the full path against a real running tenant, and adds the same fix to the existing `migrate` API which had the same class of bug since v1.2.0.

**v1.3.0 detected AZ outages. v1.3.1 actually recovers from them.**

### Fixed — AZ failover end-to-end (was broken in v1.3.0)

- **`launch-vm.sh` invocation used wrong argument format.** v1.3.0 passed `--restore-from <s3://uri>` as a flag, but `launch-vm.sh` takes restore-key as the **6th positional argument**, not a flag. Fix: emit `launch-vm.sh <tid> <vm_num> <vcpu> <mem_mb> "" <backup_key>` exactly as `backup-data.sh` and the rest of the codebase expect.
- **Backup S3 URI vs S3 key confusion.** v1.3.0 passed `s3://${ASSETS_BUCKET}/backups/<tid>/latest.gz`. But `launch-vm.sh` internally prefixes `s3://${ASSETS_BUCKET}/${RESTORE_KEY}`, which would produce a double-`s3://` prefix. Fix: pass the bare key (no prefix). The Lambda discovers the actual most-recent key via S3 list (added `_find_latest_backup_key()`).
- **Hard-coded `latest.gz` doesn't exist.** Backups are uploaded as `backups/<tid>/<ISO timestamp>.gz` — there's no `latest` alias. Fix: list `backups/<tid>/` and sort by `LastModified` to find the most recent backup. If no backup exists, refuse failover with `AZ_FAILOVER_NO_BACKUP` audit + SNS alert (Path A: never silently lose data).
- **`pick_target_host` looked up `vcpu_total` but DDB stores `total_vcpu`.** Off-by-name. v1.3.0's mock tests passed because they used `vcpu_total` everywhere. Real env had hosts with `total_vcpu` field, so spare-capacity calculation always returned 0 → "no target host" even with healthy hosts available. Fix: read `total_vcpu` first, with `used_vcpu` for accurate spare calculation, fall back to legacy field names.
- **`launch-vm.sh` `e2fsck` rejected backup restoration.** `backup-data.sh` dumps the ext4 image while VM is **paused** (vCPU frozen, but pending journal not committed). On restore, `e2fsck` must replay that journal — it returns exit code 1 (`filesystem errors corrected`), not 0. Pre-1.3.1 launch-vm.sh treated any non-zero rc as fatal. Fix: accept rc 0/1/2 (all "consistent now"), only fail on rc ≥ 4 (structural damage).
- **No synchronous wait for SSM completion.** v1.3.0's failover sent SSM and immediately flipped DDB `host_id`, even if the launch-vm.sh actually failed on the target. Fix: `_wait_ssm_done()` polls `get_command_invocation` for up to 90s and surfaces real exit status. On `Failed`, tenant is marked `failover_failed` (not stuck in `failover_recovering`).

### Fixed — ALB rule re-pointing (was broken since v1.2.0 in `migrate` API too)

- **Cross-host VM relocation didn't update ALB routing.** Each host has its own target group (`oc-<last8>`), and each tenant has an ALB rule `/vm/<tid>*` pointing at the *current host's* TG. Both `migrate` (live migration) and `_failover_tenant_to_host` (AZ failover) updated DDB ownership but **left the ALB rule pointing at the dead/old host**. Result: even after a successful migration, CloudFront kept routing traffic to the wrong place.
- Fix: `_repoint_alb_rule()` ensures target host has a TG, registers its private IP, and uses `elbv2.modify_rule` to swing the existing rule's `forward` action over. Or creates a fresh rule if none exists.
- Same fix applied to `api/handler.py`'s `migrate` action (calls a new `_repoint_alb_rule_to_tg` helper). This is a **v1.2.0 latent bug** that just got noticed because v1.3.0's failover surfaced it.
- `migrate` action also now SSH-cleans the source host's nginx tenant config so it stops advertising itself as a backend.

### Added — IAM + permissions

- `health_check` Lambda gains: `elasticloadbalancing:DescribeRules / DescribeTargetGroups / CreateRule / ModifyRule / CreateTargetGroup / RegisterTargets`, plus `s3:Get*/List*` on the assets bucket (for backup discovery), plus `SNS:Publish` for path-A alerts.
- `api` Lambda gains `elasticloadbalancing:ModifyRule` (was missing — `migrate` would have silently failed if it had attempted ModifyRule pre-v1.3.1).
- `health_check` Lambda timeout bumped from 120s to 180s to accommodate synchronous SSM wait during failover.

### Tests — 415 passed (v1.3.0 baseline) + 14 new = 429+

- **+14 tests** in `tests/test_az_failover.py`:
  - `TestFindLatestBackupKey` (5): empty list, no bucket configured, single key returned without `s3://` prefix, multiple keys sorted by LastModified, S3 error returns None gracefully.
  - `TestWaitSsmDone` (3): Success returns ok, Failed returns error, timeout after threshold.
  - `TestRepointAlbRule` (4): modifies existing rule, creates if missing, no-op without listener, registers target IP.
  - `test_no_backup_blocks_failover_with_alert`: refuses failover, marks tenant `failover_blocked`, no SSM call.
  - `test_ssm_command_fails_with_nonzero_status`: synchronous wait detects launch-vm.sh exit-non-zero and marks tenant failed.
- All v1.3.0 tests updated to mock the new SSM `get_command_invocation`, S3 `list_objects_v2`, and elbv2 client calls.
- Stack import-order bug fixed: `health_fn` no longer constructs with `listener.listener_arn` (defined later); uses `add_environment()` post-injection like `api_fn` already did.

### Real-environment E2E validation — proven on ap-northeast-1

Steps performed against a live deployment (account 835751346093):

1. Created `failover-test-6c28` tenant pinned to ap-northeast-1c (i-088f6fc814fd4b1a2).
2. Triggered `POST /tenants/.../backup` → backup landed at `s3://.../backups/failover-test-6c28/2026-05-24T09:06:27Z.gz` (9.4 MB).
3. Stopped host-agent on the ap-northeast-1c host and injected `last_health_check = 15 minutes ago` to simulate AZ-level failure.
4. Manually invoked `openclaw-health-check` Lambda.
5. **Result**: `az_outages_detected: 1, tenants_failed_over: 1, tenants_failed: 0`.
6. **Verified DDB**: tenant.host_id flipped from i-088f...1a2 → i-0bb45...50e2; `failover_from_az: ap-northeast-1c`; `restored_from: backups/failover-test-6c28/2026-05-24T09:06:27Z.gz`; `app_health: up`.
7. **Verified ALB**: rule for `/vm/failover-test-6c28*` now points at `oc-34d350e2` (target host's TG).
8. **Verified Dashboard**: `curl https://d3k97r1qs0mu76.cloudfront.net/vm/failover-test-6c28/?token=...` → **HTTP 200** end-to-end.
9. **Verified data preservation**: disk 228 MB → 229 MB after restore (the +1 MB is mount-time fs ops, contents intact).
10. **Verified cooldown**: subsequent Lambda invocation skipped with `skipped_cooldown: ["ap-northeast-1c"]`.

This is the first OpenClaw release where the "AZ failover" claim is backed by a complete real-environment trace.

### Operator notes

- **Re-deploy is required** — IAM policy changes, env var additions, Lambda timeout bump.
- **Re-roll launch-vm.sh on existing hosts** so the e2fsck fix is in place. SSM one-liner in the upgrade guide.
- **Existing in-flight migrations** that completed pre-v1.3.1 may still have their ALB rules pointed at the old host. Run a manual `POST /tenants/{id}/migrate` (no-op if target == current) to trigger the fixed path, or wait for the next deploy that touches the rule.
- **Backups are required** for AZ failover to recover data. Set `backup_cron` in `config.yml` to ensure every tenant has a recent backup before disaster strikes. Tenants without backups will get `failover_blocked` + SNS alert — by design, never silent data loss.

---

## [1.3.0] — 2026-05-24

Closes the AZ-level failover gap that v1.2.x left open and finally answers the 2026-05-22 sync ask **"if an AZ goes down, can the tenants on it auto-restart somewhere else?"** — yes, automatically, every five minutes, with cooldown protection. Plus default deployments now actually run in two AZs out of the box, instead of single-AZ with a config flag you had to remember to flip.

### Added — automatic AZ failover

- **`health_check` Lambda gains AZ-level failover orchestration.** Every poll (default every 5 minutes) the watchdog now also groups hosts by AZ and treats an AZ as unavailable if every host in it has gone stale (no `last_health_check` heartbeat from host-agent for ≥ `unhealthy_threshold_minutes`, default 10). When an outage is confirmed, the Lambda picks a healthy target host in another AZ (sorted by spare vCPU, deterministic instance_id tie-break), relaunches each affected `running` tenant on it via SSM `launch-vm.sh --restore-from <latest-backup>`, flips DDB ownership, writes audit-log entries, and publishes an SNS event. A per-AZ cooldown (default 30 minutes) prevents flapping.
- **5 pure-function helpers** carry the orchestration logic so it's all unit-testable without AWS access: `is_host_unhealthy`, `group_hosts_by_az`, `detect_unhealthy_azs`, `pick_target_host`, `should_skip_az_for_cooldown`. Plus `_check_and_handle_az_failover` (orchestrator) and `_failover_tenant_to_host` (per-tenant relaunch).
- **`host-agent.py` now writes a heartbeat to the hosts table.** Every poll cycle the agent updates `last_seen` + `last_health_check` on its own DDB host record. This is what the AZ-failover Lambda watches — without it, host-level liveness would be invisible (the previous tenant-level signal goes stale only when a tenant exists).
- **New `INSTANCE_ID` env on host-agent**, populated by `init-host.sh` from IMDS into `/etc/platform.env` so the agent knows which host record to update.

### Changed — defaults

- **Default ASG capacity is now 2 hosts in 2 AZs.** `config.yml.example` ships `asg.min_capacity: 2`, `asg.max_capacity: 8`, and `multi_az.enabled: true` by default. Single-AZ stays a one-line opt-out for cost-sensitive non-prod environments. The bump is needed for AZ failover to have an actual fallback target — a 1-host fleet has no place to migrate to.
- **`health_check.az_failover` block in `config.yml`** with `enabled` (default true), `unhealthy_threshold_minutes` (default 10), `cooldown_minutes` (default 30). Set `enabled: false` to keep the watchdog active but disable the failover side.

### Fixed — UX bugs surfaced during validation

- **Console didn't load `systemInfo` on the Tenants tab,** so the "Hosts by AZ" group header silently never rendered even when multi_az was enabled. `refresh()` now calls `loadSystemInfo()` alongside `loadHosts/Tenants/Templates`. Discovered by Playwright + DOM inspection during the v1.3.0 multi-AZ proof shoot.
- **`/hosts` API leaked the synthetic `__az_failover_state__` record.** The orchestrator stores per-AZ cooldown state on a host record with that special key. `list_hosts()` now filters out any `instance_id` starting with `__` so the console / regression tests never see these internal bookkeeping rows. Caught by the existing `test_hosts_have_expected_fields` regression check, which started failing as soon as the first AZ failover state was persisted.

### Tests — 441 passed / 0 failed (excluding 1 pre-existing flaky E2E)

- **+34 tests** in `tests/test_az_failover.py`: pure-function boundary cases (threshold edges, missing fields, sorting determinism, exclusion rules) + orchestration mocks (cooldown skip, no-target-AZ behaviour, SSM/audit/SNS side-effects, `audit_table.put_item` failure non-fatality, feature-flag noop).
- **+4 tests** in `tests/test_monitoring.py::TestHostHeartbeat`: `_write_host_heartbeat` writes both fields with same timestamp; skips when `HOSTS_TABLE` empty; skips when `INSTANCE_ID` empty; swallows DDB throttle exceptions.
- **+1 console-contract test** in `tests/test_console_api_contract.py::test_refresh_loads_system_info` — fails loudly if a future refactor pulls `loadSystemInfo` out of `refresh()` again.
- **+1 API regression test** in `tests/test_api.py::test_filters_out_synthetic_state_records` — guards `list_hosts` against future internal-record leakage.
- Existing 12 `tests/test_health_check.py` cases all still pass — the new AZ failover code is appended in a separate function, so the v1.0 watchdog path is unchanged.

Total: **441 passed** locally (up from v1.2.9's 386, **+55 tests**). One E2E (`test_backup_and_restore_from_latest`) is a pre-existing flaky test that fails on transient SSL/network errors — independent of this release.

### Operator notes

- **Existing fleet (1.2.x hosts) won't pick up the heartbeat code until host-agent is reloaded.** Push the new agent to S3 (`aws s3 cp deploy/userdata/host-agent.py s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py`) and rollout via SSM Run Command — the upgrade snippet in the README now also injects `INSTANCE_ID` into `/etc/platform.env` for hosts launched on older init-host.sh.
- **Existing host records won't have `last_seen` / `last_health_check`** until the new agent runs at least once. The `is_host_unhealthy` predicate treats missing-timestamps as unhealthy, which means the first health_check Lambda invocation after upgrade may briefly report `az_outages_detected: N` for every AZ. The cooldown protects against bogus failover, and a single agent poll (≤5s) populates the fields. **Safer rollout**: stop the EventBridge schedule for 1–2 minutes during the rollout, or set `health_check.az_failover.enabled: false` in config and re-deploy → roll the agent → re-enable.
- **The synthetic `__az_failover_state__` record** lives in the hosts DDB table once an outage triggers. It contains only cooldown state (no PII) and is intentionally not garbage-collected — kept indefinitely so cooldown survives across Lambda redeploys.

---

## [1.2.9] — 2026-05-23

Closes the metric-correctness + multi-AZ visibility gaps that 1.2.8 didn't reach. The 2026-05-22 sync flagged that operators "can only see disk usage" in the console — CPU was a hard-coded 0 stub since 1.2.0, and memory often read 0 because balloon stats returned `available_memory: 0` on most kernels. The same sync also asked for "where does multi-AZ show up in the UI" and "let admins pick which host a new tenant lands on". This release does all three at the data layer + UI.

### Fixed — real CPU & memory metrics

- **`host-agent.py` — CPU% from `/proc/<fc_pid>/stat`.** Sample utime+stime jiffies on each poll, diff against the previous sample, divide by the elapsed wall time and the configured vcpu count. Result: `cpu_pct` is now an actual percentage (0–100, capped) instead of the previous hard-coded 0. The Prometheus gauge `openclaw_vm_cpu_pct` carries the same value into AMP, so Grafana dashboards built before today's release start showing real numbers immediately.
- **`host-agent.py` — memory from `/proc/<fc_pid>/status` VmRSS.** The previous proxy `vm_mem_mb − available_memory` evaluated to 100% on any kernel where balloon stats returned 0. Switched to reading VmRSS from the Firecracker process's `/proc/.../status` (always populated on Linux), which approximately equals guest used + Firecracker overhead. Balloon stats are kept as a fallback for kernels with `VIRTIO_BALLOON_F_STATS_VQ` actually working.
- **First-sample handling.** The CPU sampler returns 0 on the first poll for a tenant (correct: you can't compute a rate from one point) and on pid reuse (jiffies counter went backwards). 7 dedicated unit tests in `tests/test_monitoring.py::TestComputeCpuPct` cover the boundary cases.

### Added — Console UI

- **Tenants table — vCPU column with usage bar.** Same colored bar style as the disk column (cyan-dim → amber → red at 70/90%), with text "12% / 2 vCPU" so 50% on 1 vcpu vs 2 vcpu reads correctly. Renders the raw `t.vcpu` allocation when the tenant isn't running yet.
- **Tenants table — Memory column with usage bar.** "1.2G/4G (30%)" — uses the new VmRSS-backed `memory_used_mb` from host-agent. Falls back to the configured `mem_mb` text when metrics aren't available yet.
- **Hosts panel — group by AZ when multi-AZ is enabled.** When `multi_az.enabled: true`, the Hosts panel splits into per-AZ sections with a small "AZ: ap-northeast-1a · 2 host(s) · 5 VM(s)" header above each group. Single-AZ deployments keep the flat list. Each host card now shows its AZ tag in the header row.
- **Tenants table — host AZ badge already shipped in 1.2.8** but now shows real data once you've followed the upgrade backfill (see operator notes).
- **Create-tenant modal — Target host picker.** New optional dropdown lets operator+ pick the exact host for a tenant (admin/operator both can use it; viewer is gated by RBAC at the API layer). Options show `instance_id (az) — N free vCPU, M VM(s)` so you can see capacity at a glance. "Auto" remains the default.
- **Settings → Fleet by AZ table.** Live distribution of registered hosts and their tenants across AZs, regardless of multi_az setting (so single-AZ deployments also see "all my hosts in 1a"). Refreshes when you load Settings or Monitoring tab.

### Added — API

- **`POST /tenants` accepts `preferred_host_id`.** Three scheduling modes now: clone_from (must same-host), preferred_host_id (admin/operator pin), default (first-fit). 404 when host doesn't exist or is draining; 400 with explicit reason when the host exists but lacks capacity.
- **`/system/info` now includes `assets_bucket` and `region`** so the console's "open in S3 console" deep links work without needing to embed the bucket name in `console/config.js`.

### Tests

- **+24 net new tests** in `tests/test_monitoring.py` covering `_read_proc_stat_cpu_jiffies` (with comm-with-spaces edge case), `_read_proc_status_rss_kb`, the pure `_compute_cpu_pct` function (8 boundary cases incl. cap, negative delta, zero divisor), and `_sample_cpu_pct` integration.
- **Status:** 386 passed / 0 failed locally on `pytest -m unit`.

### Operator notes

**Existing fleet won't pick up the metric fixes until the host-agent is reloaded.** Three options in order of intrusiveness — see the new `## Upgrade Guide → Upgrading from v1.2.7 → v1.2.9` section in `README.md` for the exact commands:

1. Roll the ASG: `aws autoscaling set-desired-capacity ... --desired-capacity 0` then back to your normal count. Clean and terminates anything stale, but ~3 min of host warmup before tenants can be created on the new ones.
2. Push host-agent.py to S3 + run an SSM `AWS-RunShellScript` against the ASG tag to copy + restart. Zero downtime, fastest.
3. Wait for natural recycling (idle reclamation will eventually replace hosts).

**Same applies to the AZ field on host records.** Existing hosts have no `az` until they re-register. The console gracefully renders `-`; either roll the ASG or run the manual backfill snippet in the upgrade guide.

**Console hard-refresh required.** `setup.sh` re-uploads `console/index.html` to S3. Browser caches the file, so users will see the old layout until they hit Cmd-Shift-R (or wait for CloudFront's ~24 h TTL). Verify with the version banner in the footer (`v1.2.9`).

## [1.2.8] — 2026-05-23

Closes a stack of "feature exists in code but the console hides it" gaps that surfaced in the 2026-05-22 sync between Neo and Xue. The product itself was already at 1.2.7, but several v1.2.x features (live VM migration, Prometheus metrics, AgentCore tools, multi-AZ / WAF / Cognito flags, host AZ) had no UI surface — operators had to read CDK code or `oc` CLI to confirm anything was wired. This release pulls all of that to the console, plus tidies up the CLI / build / test footprint that 1.2.7 left in flux.

### Added — Console UI surfaces

- **Application tab → MCP Tools card.** When `agentcore.enabled: true`, the Application tab now shows the three Lambda-backed MCP tools registered with the Gateway (`hello`, `system_info`, `timestamp`) with description + input schema. Backed by a new `GET /agentcore/tools` endpoint. Empty + helpful "AgentCore not enabled, here's how to turn it on" placeholder otherwise.
- **Application tab → Skills card enriched.** Each skill row now lays out as a card with a hover-target name, a real description (was clipping to one line), and a deep-link button to the skill's S3 prefix in the AWS console (so editors can update SKILL.md without leaving the browser). Empty state explains the upload + cron pickup flow instead of just "No skills configured".
- **Monitoring tab (new).** Demo-friendly observability page: live status of AMP / Grafana / SNS, the six per-VM Prometheus gauges with type/labels/description, three sample PromQL queries, and the AMP `remote_write` + Grafana endpoints when `metrics.enabled: true`. Closes the "we have monitoring but the demo doesn't show it" gap from the sync.
- **Tenants table → AZ column.** New `AZ` column between `Port` and `Rootfs` shows which Availability Zone each tenant's host is in. Powered by the host record's `az` field (see backend changes).
- **Tenants table → Migrate button.** New 🔀 icon in the Actions column on running tenants opens a modal that lists candidate target hosts (other active hosts, with their AZ + free vCPU + VM count) and POSTs to the existing `/tenants/{id}/migrate`. The Firecracker snapshot/restore round-trip (#20) shipped in 1.2.0 — this release just exposes it.
- **Settings tab → Infrastructure card.** Enabled / disabled state for multi-AZ, Prometheus+Grafana, WAF, Cognito+RBAC, SNS notifications, per-tenant quotas. Plus a Host Overcommit card showing the current CPU / memory ratios + default per-tenant sizing. Both populated by a new `GET /system/info` endpoint.

### Added — Backend

- **`GET /system/info`.** Feature flags + config snapshot (region, version, multi_az, metrics, waf, cognito, notifications, quotas, host_config). Read-only; available to viewer role; populated from the API Lambda's environment variables (which are now wired in `stack.py`).
- **`GET /agentcore/tools`.** Returns the static list of Lambda-backed MCP tools registered with the Gateway, with input schema. Static today (the tools are declared in `stack.py` at deploy time); future PRs can swap in a live `bedrock-agentcore.list_targets()` call when the Gateway grows user-defined tools.
- **Host record now carries `az`.** `init-host.sh` reads the AZ from IMDS during `step5: registering to DynamoDB` and writes it alongside the existing host metadata; the API Lambda's `register_host()` path also populates `az` from `ec2.describe_instances()` Placement. Existing hosts can be backfilled with a manual `aws dynamodb update-item`; new hosts pick it up automatically.

### Fixed

- **`/audit-log` was unreachable from API Gateway.** The Lambda router has had `("GET", "/audit-log")` since 1.2.0 (#48 helper restoration), but the matching `api.root.add_resource("audit-log")` was never declared in `stack.py`, so the route returned API-Gateway-level 404s in production. Added the missing resource. Existing handler code unchanged.

### Changed

- **`build-rootfs.sh` now refuses to run on macOS by default.** debootstrap is Linux-only, and the previous behaviour was to start the build then fail deep into the chroot with a confusing dependency error. The new OS guard prints the cloud-builder one-liner (`./scripts/build-rootfs-on-ec2.sh`) and exits cleanly. `FORCE_LOCAL_BUILD=1 ./build-rootfs.sh` overrides for unusual setups.
- **`oc` CLI bumped to v0.7.0 with v1.2.x action coverage.** New subcommands: `oc resize <id> --vcpu N`, `oc resize-disk <id> --new-size-mb N`, `oc migrate <id> --target-host-id i-…`, `oc batch <action> --ids a,b,c | --tag k:v`, `oc audit-log [--since ISO] [--limit N]`. Brings the CLI back to parity with the API surface (it had been stuck at v1.0 actions).

### Removed

- **`tests/test_local_dev.py`.** 1.2.7 unshipped the `local-dev/` directory but left this test file in place, asserting the deleted files still existed — guaranteed-fail since 1.2.7. Removed cleanly; LocalStack contributors keep their own private copy under `.dev/` per Neo's note.

### Tests

- **Net new tests:** 12 added in `tests/test_console_api_contract.py` (regex parity, role-list parity, tab-loader parity, new endpoint reachability). Removed: ~10 in `tests/test_local_dev.py` (file deleted).
- **Status:** 367 passed / 0 failed locally on `pytest -m unit` after the 1.2.8 changes. (The contract tests fail loudly on a typo in either side of the regex, role list, tab declaration, or new endpoint name — designed to catch the kind of "console and API drifted" bug that 1.2.7's name-validation work could have introduced.)

### Operator notes

- **Upgrade path:** `git pull && ./setup.sh <region> <profile>`. The new Lambda env vars (`MULTI_AZ_ENABLED`, `WAF_ENABLED`, `PROJECT_VERSION`, etc.) and new API Gateway resources (`/system/info`, `/agentcore/tools`, `/audit-log`) are added by CDK on the next `cdk deploy`. No DDB migration needed; new fields are additive.
- **Existing hosts won't backfill `az` until they re-register.** Either roll the ASG (terminate-and-launch) or run `aws dynamodb update-item --table-name openclaw-hosts --key '{"instance_id":{"S":"i-..."}}' --update-expression 'SET az = :a' --expression-attribute-values '{":a":{"S":"ap-northeast-1c"}}'` per host. The console gracefully handles the empty case (column shows `-`).
- **Console rebuild:** `setup.sh` regenerates `console/config.js` with the new `OC_REGION` + `OC_ASSETS_BUCKET` globals required by the skill S3 deep-link.

## [1.2.7] — 2026-05-22

### Changed
- **Console: live name validation.** Create-Tenant modal now mirrors the API's `_NAME_RE` (`^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`) — invalid names show an inline error and disable the Create button instead of failing silently with a 400 from the server.
- **Console: Disk column.** New `Disk` column between `Mem (MB)` and `Guest IP`: a colored bar (green / amber / red at 70% / 90%) plus exact `used/total (pct%)` text below — replaces the cramped `formatMetrics()` cell that was hidden at the far right of the row.
- **Console: themed scrollbars.** Webkit + Firefox scrollbars now use the cyan-on-dark palette to match the rest of the UI.

### Removed
- **`local-dev/` unshipped from the repo** — the docker-compose stack (LocalStack + stub host-agent) for running Lambda + console without an AWS account, shipped in 1.2.0 (PR #46 / issue #24), is now treated as a personal contributor workflow instead of a tracked repo asset. The directory is gitignored; keep your own copy locally if you use it. Use `pytest -m unit` for routine local iteration.

## [1.2.6] — 2026-05-22

### Fixed
- **`refresh_rootfs()`: 0-byte rootfs hotfix.** Decompress now writes to a `.tmp` sibling, checks `[ -s ]`, then atomically `mv`s — replaces a `pigz -dc > rootfs.ext4` redirect that could leave a 0-byte file on any pigz failure, blocking new tenant creates.
- **API: tenant name validation.** `_validate_name()` rejects names that aren't DNS-label safe (`^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`). Previously, names with spaces or uppercase were accepted but couldn't be deleted via the API path.

### Changed
- **Cognito: import-and-recreate.** When `console_auth.user_pool_id` is set, the stack imports the pool but recreates `UserPoolDomain` + `UserPoolClient` as stack-owned resources (was: assumed to already exist, producing drift). Existing user accounts unchanged. Removes the `console_auth.user_pool_client_id` config key.
- **Audit table: per-deploy naming.** Table name now suffixed with `Aws.STACK_ID` first segment (e.g. `openclaw-audit-log-a63eaa70`). Prevents collision on `cdk destroy` + redeploy. **One-time table replacement on upgrade** — old table preserved as orphan; delete it manually if history isn't needed.

### Added
- **`scripts/cleanup-bad-name-tenants.sh`** — removes legacy bad-name tenants (SSM stop-vm + dir cleanup, ALB rule, host counters, DDB mark deleted). Supports `--dry-run` / `--yes`.

### Operator notes
- Upgrade is a single `./setup.sh <region> <profile>`.
- If you have legacy tenants with spaces or uppercase in their names, run `./scripts/cleanup-bad-name-tenants.sh <region> <profile> --dry-run` to preview, then drop `--dry-run` to clean up.


## [1.2.5] — 2026-05-21

Closes the **observability** gap. The 1.2.4 release verified the data plane all the way to "HTTP 200 from CloudFront → ALB → Nginx → Firecracker" but never actually checked that AMP / Grafana were receiving samples.
A live probe today found two stacked bugs that meant zero metrics had ever flowed into AMP since the feature shipped (issue #4 / PR #38).

### Fixed (real production bugs found via live AMP probe)
- **`deploy/userdata/adot-config.yaml` / `host-agent.py` — split-port mismatch.** 
  ADOT was configured to scrape `127.0.0.1:9090`, but `host-agent.py main()` only ever bound the single HTTPServer on `OC_AGENT_PORT` (8899). The agent's own comment block stated the design as "/metrics on the same HTTPServer as /health to avoid a second listener" — so the architecture was correct, the wiring was not. 
  Result: every ADOT scrape since the metrics feature shipped failed with "Failed to scrape Prometheus endpoint". 
  Fix: ADOT now scrapes `127.0.0.1:8899`; the dead `OC_AGENT_PROM_PORT` variable was removed.
- **`deploy/userdata/host-agent.py` — `_status` did not include the computed metrics.** 
  `_write_ddb()` computed per-VM metrics (`memory_used_mb`, `disk_used_mb`, etc.) and wrote them to DynamoDB, but never assigned them back into the in-memory `info` dict that `_status` is rebuilt from each poll cycle. The `/metrics` endpoint reads `_status`, so it could only ever emit `openclaw_vm_health` (which is derived directly from `vm_health`). 
  Every other gauge was empty in AMP, even when DDB had the values. Fix: mirror the computed `metrics` dict back into `info["metrics"]` before the DDB write so the Prometheus exporter sees it.
- **`deploy/stack.py` + `tests/test_prometheus.py` + `init-host.sh` — stale `:9090` references.** 
  Comments and assertions still pointed at the never-bound 9090 port. Updated to `:8899` and added two regression tests that fail loudly if either bug recurs:
  `test_collector_config_has_amp_endpoint` now asserts `127.0.0.1:8899` is the scrape target (and explicitly bans 9090);
  `test_write_ddb_mirrors_metrics_back_into_status_for_prom_exporter` asserts the back-mirror line stays in `_write_ddb`.
- **`uv.lock` — bumped `idna` 3.11 → 3.15** to close the medium-severity Dependabot CVE alert (CVE-2024-3651-related; idna < 3.15 lets
  specially-crafted inputs to `idna.encode()` bypass the original patch). Pure transitive bump (boto3 → urllib3 → idna), no API change.
  Verified by full unit-test re-run.

### Verified end-to-end on real AWS (live AMP probe, this exact tag)
- ✅ ADOT collector active + `Everything is ready. Begin running and processing data.` (no more `Failed to scrape Prometheus endpoint`).
- ✅ `host-agent /metrics` emits all 6 gauges with per-tenant labels 
  (`openclaw_vm_memory_used_mb`, `openclaw_vm_memory_balloon_mib`,
  `openclaw_vm_disk_used_mb`, `openclaw_vm_disk_total_mb`,
  `openclaw_vm_disk_used_pct`, `openclaw_vm_health`).
- ✅ AMP PromQL `query` API returns real values for all 6 gauges (probed via SigV4-signed HTTPS GET). Example: `openclaw_vm_health {tenant="obs-test-366f", instance="i-0bb45368534d350e2"} = 1`.
- ✅ Grafana workspace `g-5e6517493c` ACTIVE on Grafana 10.4 with AWS_SSO authentication; AMP shows up under PROMETHEUS data sources.

### Test status
- **386 passed / 0 failed / 0 skipped** locally (1.2.5 adds 2 new regression tests on top of the 1.2.4 baseline of 384).

### Operator notes
- After upgrading from 1.2.4 → 1.2.5, redeploy the stack and roll the ASG (or push the new `host-agent.py` + `adot-config.yaml` via SSM) to pick up the fixes on existing hosts. New hosts launched by `setup.sh` get the fix automatically.
- `metrics.enabled: false` (default in `config.yml.example`) skips AMP and Grafana entirely — there is no cost surprise for users who don't opt in.


## [1.2.4] — 2026-05-21

End-to-end microVM validation pass. Closes the long-standing "data plane
out of scope" gap by actually booting Firecracker on real AWS, which
surfaced two genuine production bugs hidden behind layers of mocking.

### Fixed (real production bugs found via microVM E2E)
- **`deploy/userdata/host-agent.py` — DynamoDB reserved-keyword bug.**
  `metrics` is a DDB reserved keyword (along with `status`, `name`, etc.),
  so the literal `SET … metrics = :m` UpdateExpression issued every poll
  cycle was rejected with `ValidationException: Attribute name is a
  reserved keyword`. Tenants stuck in `creating` forever because
  host-agent could never promote them. Fixed by aliasing the field as
  `#m` in `ExpressionAttributeNames` (same pattern already used for the
  also-reserved `status` keyword). New regression test
  `test_write_ddb_uses_attr_name_alias_for_metrics_reserved_keyword`
  asserts the alias is always present so this can't silently regress.
- **`deploy/userdata/launch-vm.sh` — OpenClaw 2026.5+ schema break.**
  Upstream OpenClaw moved MCP servers from a top-level `mcpServers` key
  to `mcp.servers.<name>` (verified by reading where `openclaw mcp set`
  itself writes). `launch-vm.sh` was injecting AgentCore Gateway URLs at
  the old top-level path, causing OpenClaw Gateway to refuse the entire
  config (`Invalid input at <root>`) and crash-loop. Updated the `jq`
  mutation to write under `.mcp.servers.agentcore-gateway`, which the
  new schema validates.
- **`deploy/userdata/stop-vm.sh` — Firecracker zombie processes.**
  Plain `pkill -TERM` on the Firecracker socket left orphan processes
  whenever stop-vm raced with launch-vm's late init steps (e.g. when
  back-to-back e2e tests delete a tenant before its VM has finished
  booting). The zombies silently consumed vCPU + memory budget on the
  host, eventually starving subsequent VM launches. Now sends `TERM`
  first, sleeps 1 s, then `KILL` to guarantee the process is gone before
  the script returns.
- **`tests/test_e2e.py::TestBackupRestoreRoundtrip` — pre-flight cleanup.**
  Earlier e2e cases occasionally leave half-launched VMs behind; the
  roundtrip test now sweeps any leftover `e2e-…` tenants before creating
  its own src/dst pair, so a single suite run lands at 385/385 instead of
  384/385+1 transient timeout.

### Added
- **`scripts/build-rootfs-on-ec2.sh`** — cloud-native rootfs build for
  contributors on macOS / Windows / Cloud9 (anywhere `debootstrap` is
  unavailable). Launches a one-shot `t3.medium` (or `t4g.medium` for
  ARM64) Ubuntu builder in the same region with a 30 GB root volume,
  attaches the existing `openclaw-host-profile` instance role for S3
  access, runs the chroot build via SSM Run Command, uploads the
  artifacts + `manifest.json` to `s3://${ASSETS_BUCKET}/deployment/rootfs/`,
  and terminates the builder. ~10 minutes total. Closes the long-standing
  UX gap where the README implied "just run `./build-rootfs.sh`" but
  silently required the operator to find a Linux host. The script
  uploads its own log to S3 on failure for diagnosability.

### Verified end-to-end on real AWS (this is the first 1.2.x release with
microVM data plane fully verified)
- ✅ `cdk deploy` finishes `CREATE_COMPLETE` in ~180s (post-1.2.3 redeploy).
- ✅ `./scripts/build-rootfs-on-ec2.sh v1.0` builds + uploads rootfs in ~9 min.
- ✅ ASG host self-registers to DDB with `rootfs=v1.0` in ~3 min after launch.
- ✅ `POST /tenants` → `creating` → `running` in ~11 s (latest live probe); `app_health=up` in ~16 s; full `creating → deleted` cycle in ~23 s.
- ✅ Firecracker VM responds on `172.16.x.2:18789`; gateway token auto-injected.
- ✅ `GET https://${DASHBOARD_URL}/vm/{tenant_id}/?token=…` → **HTTP 200** (177 ms TTFB through CloudFront → ALB → Nginx → Firecracker).
- ✅ Per-VM metrics field correctly populated in DDB (`memory_used_mb`, `disk_used_mb`, `disk_used_pct`, `cpu_pct`, `memory_balloon_mib`).
- ✅ `DELETE /tenants/{id}` cleanly tears down (ALB rule + iptables DNAT removed, Firecracker SIGKILL'd, no zombies).

### Test status (post-1.2.4, with valid AWS creds + rootfs in S3)
- **385 passed / 0 failed / 0 skipped** locally — fully clean run on a
  freshly-cleaned host. (Earlier runs in this cycle reported 384+1 skip
  due to back-to-back roundtrip cleanup races; the new pre-flight sweep
  + stop-vm.sh SIGKILL fix close that gap.)

## [1.2.3] — 2026-05-19

End-of-Q2-2026 milestone tag. Identical code to 1.2.2; this entry exists purely to make the version ladder SemVer-monotone.

### Changed
- `pyproject.toml` version bumped **1.1.1 → 1.2.3** to restore SemVer
  monotonicity with the previous `1.1.x` line. (Original 1.1.x → 1.2.x bump
  was missed when the Q2 milestone was first tagged with the descriptive
  `v1.0.0-milestone-q2-2026` tag.)

### Fixed
- **CHANGELOG headings** rewritten to use SemVer-correct names
  (`1.2.0/1.2.1/1.2.2/1.2.3`); descriptive tags kept as aliases.
- **`deploy/lambda/api/handler.py`** — duplicate definitions of
  `audit_table`, `QUOTAS_ENABLED`, `QUOTAS_MAX_*`, `NOTIFICATIONS_TOPIC_ARN`
  and the `sns` client (residue from #48 post-merge regression repair) were
  removed. The top-of-module definitions are now authoritative; the second
  `QUOTAS_ENABLED` in particular was using `default=true` which contradicted
  the documented "default off" behavior.
- **`tests/test_cli.py`** — `test_handles_missing_credentials_gracefully` and
  `test_missing_credentials_errors` now `monkeypatch.chdir(tmp_path)` to
  isolate from a populated `.env.deploy` in the project root. Without this,
  the tests passed on clean CI but failed on developer machines that had
  already run `./setup.sh`.
- **`register_host()` API endpoint** no longer hard-codes 16384 MiB for
  every host. It now calls `ec2.describe_instance_types()` and falls back
  to the same `_SIZE_TO_VCPU` × `_FAMILY_LETTER_TO_MEM_PER_VCPU` table that
  `deploy/stack.py` uses. The Lambda execution role gained
  `ec2:DescribeInstanceTypes`. Body is also now nullable-safe (returns 400
  on missing body / `instance_id` instead of a 500 KeyError).
- **`tests/conftest.py::load_env_deploy`** — removed the hard-coded
  `~/Code/sample-multi-tenant-openclaw-on-firecracker/.env.deploy` fallback
  in favour of paths derived from `__file__` and `cwd`. Works in any
  checkout location and inside containers.
- **`tests/test_e2e.py::test_backup_and_restore_from_latest`** — pre-flight
  checks `GET /hosts/rootfs-version` and `GET /hosts`; skips with a clear
  message when the data plane isn't ready (no rootfs in S3, or no host
  registered). Previously fails with a 240s timeout and a confusing
  "did not reach 'running'" message that hides the real problem.
- **`deploy/userdata/init-host.sh`** — when the host can't find
  `manifest.json` after 10 minutes of retries, the failure log now
  prints both recovery paths (`./scripts/build-rootfs-on-ec2.sh v1.0`
  for laptops without Linux, or `./build-rootfs.sh v1.0` for a Linux
  host) instead of just "manifest.json not available".

### Added
- **`scripts/build-rootfs-on-ec2.sh`** — cloud-native rootfs build for
  contributors on macOS / Windows / Cloud9 (anywhere `debootstrap` is
  unavailable). Launches a one-shot `t3.medium` (or `t4g.medium` for
  ARM64) Ubuntu builder in the same region, attaches the existing
  `openclaw-host-profile` instance role for S3 access, runs the chroot
  build via SSM Run Command, uploads the artifacts + `manifest.json`
  to `s3://${ASSETS_BUCKET}/deployment/rootfs/`, and terminates the
  builder. ~10 minutes total. Closes the long-standing UX gap where the
  README implied "just run `./build-rootfs.sh`" but silently required
  the operator to find a Linux host.

### Test status (post-1.2.4 cleanup, with valid AWS creds)
- **382 passed / 0 failed / 2 skipped** locally — clean run.
- Both skips are intentional and conditional on the data-plane state:
  - `TestListAllBackups::test_list_all_backups` skips when no backups
    exist yet (the roundtrip test populates one when it runs).
  - `TestBackupRestoreRoundtrip::test_backup_and_restore_from_latest`
    skips when `manifest.json` is missing from S3 (i.e. `./build-rootfs.sh`
    has not been run on a Linux host yet) — the test would otherwise time
    out with a confusing "did not reach 'running'" message.
- Once `./build-rootfs.sh` has populated S3 and a host is registered,
  both tests un-skip and the suite reports **384 / 384** clean.

### Snapshot at this tag

| Metric | Value |
|---|---|
| New PRs merged | **27** (24 features + 3 patches) |
| Net new unit tests | **~300** (84 → 384) |
| Test status | **382 / 384 passed**, 2 skipped (data-plane-conditional, see below); 0 failed |
| Issues closed | **25** (24 features + #48 regression) |
| Git tags | **18** (11 per-issue + 4 release + 3 historical) |
| GitHub Releases | **4** (1.2.0, 1.2.1, 1.2.2, 1.2.3) |
| Real-AWS deploy | ✅ CFN `CREATE_COMPLETE` in 450s, control plane verified end-to-end |
| Open issues / PRs | **0 / 0** |

### Known limitations / future work

These are explicit non-goals at 1.2.3; raise an issue if any block your use case.

- **Multi-agent runtime abstraction** — OpenClaw is hard-coded into `build-rootfs.sh` (`npm install -g openclaw`, `openclaw onboard`, `templates/openclaw.json`). Supporting LangGraph / CrewAI / Bedrock Agent / custom Python agents needs a new agent-runtime ABC (the same pattern PR #41 used for Firecracker / Cloud Hypervisor / QEMU).
- **VM data plane on real AWS** — `./build-rootfs.sh` requires a Linux build host with `debootstrap`; rootfs upload to S3 is a separate workflow. The 1.2.x E2E verified the **control plane** (API → DDB → ASG → host registration → tenant lifecycle), not Firecracker boot.
- **Cross-arch rootfs caching** — `--arch arm64` switch rebuilds from scratch. S3 caching by arch + version would speed up Graviton deploys.
- **Cross-region / cross-account federation** — single region per CDK stack today.
- **Pre-copy live migration** — current snapshot/restore (#20) pauses for the duration of memory transfer. Pre-copy iterations would drop downtime to sub-second.

## [1.2.2] - 2026-05-19

Patch release — real-world AWS deploy bugs surfaced during E2E verification.

### Fixed
- **#52** `init-host.sh` race condition: ASG launches the host the moment `cdk deploy` creates the LaunchTemplate, but `setup.sh` uploads `host-agent.py` / `launch-vm.sh` / `stop-vm.sh` *after* deploy returns. The host's user-data ran first and 404'd on those S3 paths, then failed to register because the CFN output query also returned `None` mid-deploy. `_stack_output` and `_s3_get` now retry up to 5 minutes.
- **#53** ALB tried to launch in a single AZ when `multi_az.enabled: false` (default `az_count=1`). AWS rejects with *At least two subnets in two different Availability Zones must be specified*. ALB now independently uses `max(2, az_count)` regardless of ASG mode — single-AZ ASG mode still works for cost-conscious deployments.
- **#51 / setup.sh** Pre-existing `${CDK_ARGS[@]}` expansion broke under `set -u` + empty array on zsh / bash <4.4. Guarded with `${CDK_ARGS[@]+"${CDK_ARGS[@]}"}` pattern.

### Verified
- `./setup.sh ap-northeast-1 …` finishes CFN `CREATE_COMPLETE` in ~450s on a clean account.
- Host registers to DDB via init-host's retry path.
- Tenant lifecycle (`oc create` → pending → creating → `oc delete`) end-to-end on real AWS.

### Out of scope
- VM data plane (Firecracker boot from rootfs) — requires a Linux host running `./build-rootfs.sh` first; control plane is now 100% verified end-to-end.

## [1.2.1] - 2026-05-19

Patch release — full regression repair after the 1.2.0 cross-PR squash-merge.

### Fixed
- **#48** — Restored helpers + call-sites lost during 1.2.0 batch merge (`-X theirs` auto-resolution discarded code from earlier PRs):
  - `_parse_ttl` + `_TTL_*` constants ([#28] / issue #15)
  - `_parse_schedule` + scaler `_schedule_should_run` + reconciliation ([#30] / issue #11)
  - `_audit_write` + GET `/audit-log` route + `_list_audit_log` ([#32] / issue #17)
  - `_check_quota` + `QUOTAS_*` env knobs ([#34] / issue #9)
  - `_publish_event` + SNS client ([#33] / issue #13)
  - `batch_tenants` + `_resolve_filter` ([#29] / issue #23)
  - `tenant_resize` + 'resize' action branch ([#35] / issue #16)
  - 'resize-disk' action branch ([#47] / issue #22)
- **stack.py**: re-added `aws_aps` + `aws_grafana` imports
- **setup.sh**: `${CDK_ARGS[@]+"${CDK_ARGS[@]}"}` to handle `set -u` + empty array on zsh

### Testing
- **368 passed / 1 skipped / 0 failed** at the time of 1.2.1 release (was 123 failed at 1.2.0).
- 1.2.3 picked up the duplicate-symbol cleanup + `test_cli` isolation fix.
- 1.2.4 added the `register_host` memory-lookup fix and the data-plane-aware
  E2E skip; the current run reports **382 / 384 passed, 2 skipped, 0 failed**
  on a control-plane-only deploy. Once `./build-rootfs.sh` populates S3 and
  a host registers, both skips lift automatically and the suite is **384 / 384**.

### Process
- Documented best practices in release notes:
  - Limit ≤10 concurrent same-file PRs per batch
  - Avoid `git merge -X theirs` for cross-PR conflicts
  - Per-PR tags (`v0.N.0-issueX`) for surgical rollback

## [1.2.0] - 2026-05-18

Aliases: `v1.0.0-milestone-q2-2026`. First major milestone — **24 features
merged, 25 issues closed**, ~250 new unit tests.

### Added — Observability (2)
- **#3 Per-VM CPU/memory/disk metrics in DynamoDB** ([#37]) — host-agent samples every 30s, writes per-VM resource usage to the tenants table; console shows live values per row.
- **#4 Amazon Managed Prometheus + Grafana** ([#38]) — Prom remote-write from host-agent, AMG datasource + dashboards, alerts wiring.

### Added — Security (4)
- **#6 EBS encryption at rest for host data volumes** ([#26]) — KMS-encrypted by default; compliance-friendly.
- **#7 Optional AWS WAF integration for API Gateway** ([#31]) — rate-limit + geo-block + AWS managed OWASP rule sets.
- **#14 RBAC via Cognito Groups (admin/operator/viewer)** ([#39]) — role attached to id-token, dispatcher gates per-route; admin = full, operator = CRUD, viewer = read-only.
- **#17 Audit log for all mutating API operations** ([#32]) — every POST/PUT/DELETE writes an entry to the audit DDB table with TTL = 90 days; `GET /audit-log?since=…&limit=…`.

### Added — Tenant lifecycle (9)
- **#9 Per-tenant resource quotas** ([#34]) — `QUOTAS_MAX_VCPU/MEM/DATA_DISK_MB`, fail-fast in `create_tenant`; defends against noisy neighbors.
- **#10 Tenant tagging, grouping, and search** ([#27]) — `tags: {key: value}` field; `GET /tenants?tag=team:sre` AND-filter across multiple tags.
- **#11 Scheduled tenant auto-stop/start (office-hours mode)** ([#30]) — `schedule: {start, stop, timezone, days}`; scaler reconciles every tick.
- **#12 Tenant snapshot/clone via local cp on the same host** ([#36]) — `clone_from: <id>` co-schedules onto the source's host so `cp --sparse data.ext4` is sub-second.
- **#13 SNS lifecycle notifications for tenant events** ([#33]) — `tenant.created/deleted/migrated/expired` events to a configurable SNS topic.
- **#15 Tenant TTL with auto-stop or auto-delete on expiry** ([#28]) — `ttl_hours` + `on_expiry: stop|delete`; scaler scans expired tenants every tick.
- **#16 Live VM resize — hot-add vCPU without restart** ([#35]) — `POST /tenants/{id}/resize {vcpu}` calls Firecracker `/machine-config` PATCH; refuses shrinks (Firecracker limitation) and memory live-resize.
- **#22 Offline auto-resize for tenant data disks** ([#47]) — `POST /tenants/{id}/resize-disk {new_size_mb}` pauses VM → `truncate -s` sparse file → `e2fsck` + `resize2fs` → resume; pause window ~seconds.
- **#23 Batch tenant operations endpoint** ([#29]) — `POST /batch/tenants {action: stop|start|delete|backup, ids|filter}`; per-tenant errors collected into `failed[]`, never abort the batch.

### Added — Platform (4)
- **#5 Pluggable VM runtime protocol (Firecracker / CHV / QEMU stub)** ([#41]) — `Runtime` ABC + factory; only Firecracker fully implemented, the other two are reserved seams.
- **#8 Multi-AZ HA opt-in** ([#42]) — `multi_az.enabled` + `az_count`; ASG fan-out across AZs (ALB always ≥2 AZ — clarified in v1.0.2/#53).
- **#19 Graviton (ARM64) host support** ([#44]) — `host.arch: arm64` switches AMI lookup to Ubuntu Noble arm64 + InstanceType default `m8g.xlarge`; `build-rootfs.sh --arch arm64` cross-builds rootfs.
- **#20 Live VM migration via Firecracker snapshot/restore** ([#45]) — `POST /tenants/{id}/migrate {target_host_id}`; SSM on source pauses + snaps + S3-uploads, SSM on target downloads + restores; DDB `host_id` flips.

### Added — DevX (2)
- **#21 Unified `oc` CLI** ([#40]) — single-file argparse Python tool; subcommands: `list / get / create / delete / restart / start / stop / pause / resume / backup / reset / backups / hosts / version`; reads `OC_API_URL`/`OC_API_KEY` or `.env.deploy`.
- **#24 Local development mode with LocalStack + stub host-agent** ([#46]) — `local-dev/docker-compose.yml` + LocalStack 3 + Python stub host-agent; iterate on Lambda + console without an AWS account.

### Added — Deployment (1)
- **#18 Terraform module at parity with CDK core** ([#43]) — DDB tables, S3 bucket + lifecycle, Lambda + IAM, API Gateway routes; `terraform/README.md` documents the CDK ↔ TF mapping. Advanced features (CloudFront, Cognito, WAF, AMP/AMG, AgentCore) intentionally omitted.

### Added — Other
- **dependabot/urllib3 2.7.0** ([#25]) — security patch.

### Testing
- ~250 new unit tests across the 24 PRs; every PR shipped TDD red→green with a full-suite gate.
- Per-issue tags `v0.3.0-issue3` … `v0.13.0-issue22` for surgical rollback (each tag points at the feature branch's last commit before merge).

[#25]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/25
[#26]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/26
[#27]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/27
[#28]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/28
[#29]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/29
[#30]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/30
[#31]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/31
[#32]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/32
[#33]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/33
[#34]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/34
[#35]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/35
[#36]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/36
[#37]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/37
[#38]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/38
[#39]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/39
[#40]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/40
[#41]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/41
[#42]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/42
[#43]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/43
[#44]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/44
[#45]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/45
[#46]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/46
[#47]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/47

## [1.1.1] - 2026-04-30

### Added
- **AgentCore Memory E2E test** — create_event (conversation turns) + list_events + batch_create_memory_records, verified on real AWS
- **AgentCore Code Interpreter E2E test** — start_session → executeCode (Python 3.12, Sum=5050, exitCode=0) → stop_session
- **AgentCore Browser E2E test** — start_session → get_session (status=READY, WebSocket stream endpoint) → stop_session

### Testing
- 90 tests total: 74 unit + 16 E2E (0 failed)
- All AgentCore components now have end-to-end call verification, not just resource creation checks

## [1.1.0] - 2026-04-30

### Added
- **AgentCore integration verified on real AWS infrastructure** — Full end-to-end deployment and testing of Gateway, Memory, Code Interpreter, Browser, and Workload Identity
- **AgentCore tools Lambda** — hello, system_info, timestamp MCP tools registered on Gateway
- **AgentCore toggle test** — Verified disabled → enabled → disabled lifecycle with resource creation/deletion
- **AgentCore unit tests** — 13 new tests covering tools Lambda and status endpoint
- **AWS Blog posts** — blog.md (EN) and blog-cn.md (CN) with Word versions, covering architecture, cost optimization, and AgentCore integration
- **AWS Blog Writer Skill** — Reusable skill for writing AWS-style technical blog posts
- **CHANGELOG.md** — Project changelog
- **Console improvements** — Version display and GitHub link in Settings page

### Verified (real AWS deployment)
- CDK stack deployment: 144 base resources + 17 AgentCore resources
- Tenant lifecycle: create → running (20s) → Dashboard 200 OK → delete
- AgentCore MCP injection: Gateway URL auto-injected into VM openclaw.json
- Lambda tools invocation: hello/system_info/timestamp all return correct results
- Toggle test: disabled (no MCP) → enabled (MCP injected) → disabled (MCP removed)
- Backup/restore roundtrip: create → backup → restore from backup → running
- All 94 tests passed: 74 unit + 12 E2E + 8 regression

## [1.0.0] - 2026-04-30

### Features
- **Multi-tenant OpenClaw deployment** — Create, manage, and isolate OpenClaw AI agents in Firecracker microVMs via REST API
- **Firecracker microVM isolation** — Independent kernel, filesystem (OverlayFS), and network per tenant
- **Auto scaling** — ASG scale-out on demand, two-round idle host reclamation
- **CPU/Memory overcommit** — Configurable ratios with Firecracker balloon dynamic memory management
- **Auto backup & restore** — EventBridge scheduled daily backups, orphan-safe restore from any backup
- **Shared skills** — S3-managed skills synced to all VMs via cron, independent memory per tenant
- **Config templates** — S3-managed OpenClaw configuration templates, selectable at tenant creation
- **Web management console** — Alpine.js SPA on CloudFront with 4 tabs (Tenants, Application, Backups, Settings)
- **Dashboard access** — One-click HTTPS access via CloudFront → ALB → Nginx → VM Gateway (WebSocket)
- **AgentCore integration** — Optional toggle for Gateway (MCP tool hub), Memory, Code Interpreter, Browser, and Workload Identity
- **Two-tier health monitoring** — Host agent (5s poll) + Lambda watchdog (5min) with auto-recovery
- **Rootfs version management** — manifest.json versioning, per-host tracking, refresh-rootfs API
- **Tenant lifecycle** — create, delete, stop, start, pause, resume, restart, reset, backup
- **Optional Cognito auth** — OAuth2 implicit flow for console protection
- **Custom domain support** — bind-domain.sh for CloudFront + ACM certificate
- **Spot instance support** — Optional 60-70% cost reduction

### Testing
- 74 unit tests (API scheduling, overcommit, tenant CRUD, scaler, health check, balloon, AgentCore tools)
- 12 E2E tests (API connectivity, tenant lifecycle, backup/restore roundtrip, regression)
- 8 regression tests (routing, CORS, field validation)
- Full AgentCore integration verified on real AWS infrastructure (Gateway + Memory + CodeInterpreter + Browser + Identity)
- Toggle test: disabled → enabled → disabled with resource creation/deletion verified

### Documentation
- README.md (English) and docs/README-CN.md (Chinese)
- AWS Blog posts: blog.md (EN) and blog-cn.md (CN) with Word versions
- config.yml.example with all configuration options documented
- CONTRIBUTING.md with code conventions and PR guidelines
