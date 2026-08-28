# Pre-launch patch validator

`oc-prelaunch-validate` is a read-only final gate for a patch kit. It compares
the public gateway source, kit declarations, and live observations without
changing the kit, fleet, or control plane.

## Usage

```sh
patch/validator/oc-prelaunch-validate \
  --kit patch/KIT_PLACEHOLDER \
  --gateway-ref origin/gateway \
  --environment-json ./environment.json \
  --region REGION_PLACEHOLDER \
  --target-vms TARGET_PLACEHOLDER \
  --report ./prelaunch-report.json
```

Use `--group A,C` to select groups. `--offline` runs source and kit checks that
do not require AWS credentials. A missing optional `boto3` installation causes
AWS-dependent checks to report `INCONCLUSIVE`; it does not crash the tool.

Exit status:

- `0`: every reported finding is `PASS`.
- `1`: at least one finding is `FAIL`.
- `2`: no finding is `FAIL`, but at least one is `INCONCLUSIVE` or `UNVERIFIED`.

The tool writes only the requested report file. AWS access is restricted in
`lib/awsread.py`; Function execution and unapproved SSM payloads are rejected
before a client call is made.

## Environment observations

The discovery output provides account-specific coordinates. Checks also accept
additional read-only observations under these optional keys:

- `live_files`: repository path to observed `sha256`, `mode`, and `executable`.
- `scale`: source-derived capacity targets, queue URL, and measured budgets.
- `ssm.parameter_paths`, `ssm.references`, `ssm.steady_state`: parameter probes.
- `iam.roles`, `iam.policy_documents`: role-to-source mappings and policy text.
- `known_dimension_values`: dimension names mapped to observed node identifiers.
- `route_samples`: control-plane and data-plane host/port readings per tenant.
- `replica_endpoints`: parameter value plus declared and observed endpoint role.
- `probes`: probe kind, inspected object count, and transport status.
- `residue_observations`: read-only findings for test tenants, directories,
  processes, host-file entries, and tunnels.

Absent observations produce `INCONCLUSIVE` or `UNVERIFIED`, never a guessed
pass.

## Check criteria

| ID | Pass criterion | Failure meaning |
| --- | --- | --- |
| A1 | Each critical gateway file matches the manifest hash; each available live file separately matches that manifest hash. | Gateway publication or live deployment bytes do not match the kit declaration. |
| A2 | The deployed Lambda archive is non-empty and contains `core/__init__.py` and `core/auth.py`. | An overlay removed required package files. |
| A3 | The ASG pins a numeric template version, rendered user data has no active `{{TOKEN}}`, and rendered bytes match the gateway source when comparable. | The template floats, contains an unresolved token, or serves different bytes. |
| A4 | Every gateway `add_method` route exists in the deployed stage export and deployed `OPTIONS` authorization is consistent. | A route is only defined in source/resources, or preflight authorization diverges. |
| B1 | Live Lambda variables are listed as `MATCH`, `DIVERGED`, or `UNDECLARED`. Differences are informational, not an automatic failure. | This check does not assign intent; review every non-match. |
| B2 | Fleet slots, event-source throughput, deadline budget, and table write mode/capacity satisfy injected targets. | At least one source-backed capacity identity does not balance. |
| B3 | Parameter references resolve, values parse, switches equal source-declared steady state, and inferred types/enums accept the value. | A parameter can fail closed, point at the wrong role/resource, or retain a temporary state. |
| B4 | Queue visibility does not take effect later than the declared lifecycle deadline. | The customer deadline and the queue's real retry timing disagree. |
| C1 | Every AWS method call found in each role's source set simulates as allowed. | `UNDER_GRANT`: a runtime call can be denied. |
| C2 | A protected plus unrelated leading-key batch still simulates as `explicitDeny`. | An all-values condition permits a mixed batch to bypass the deny. |
| C3 | Every deployed function is queried for the last three days and no `ERROR` or `WARN` event is returned; invocation count is context only. | Recent runtime warnings/errors require investigation. |
| C4 | Alarm node dimensions name observed nodes and insufficient-data states do not exceed the alarm's own evaluation window. | An alarm targets a nonexistent node or has stopped receiving data. |
| D1 | Both manifest commit identifiers resolve through `git cat-file`. | A customer clone cannot resolve the declared baseline or patch commit. |
| D2 | No kit is completely covered by another kit on the same repository paths with equal or gateway-current bytes. | A superseded kit should be removed. |
| D3 | A push marker and manifest log both identify the current patch commit. | Publication evidence for this patch round is incomplete. |
| D4 | The kit contains no apply state, bytecode cache, or metadata residue. | Local state can leak prior-environment data or invalidate the manifest. |
| D5 | Every shell or Python script directly executed by SSM is declared mode `0755` and is executable when live evidence is available. | Correct bytes can still fail at runtime with an execution permission error. |
| D6 | Every S3 write records the previous `VersionId` with a preceding head read. | The write has no rollback anchor. |
| D7 | Kit shell libraries contain neither `mapfile` nor associative arrays. | The kit is not portable to bash 3.2. |
| E1 | Alias versions and latest state agree on code hash, environment, dead-letter configuration, and layers; serving qualifiers are reported. | The service and the edited latest version have diverged. |
| E2 | Every probe reports a nonzero inspected count, transport status is meaningful, and zero-match grep is neutralized. | A green/red result may be vacuous or a transport artifact. |
| E3 | Live file bytes and mode equal the replayable manifest declaration. | A machine-only hotfix was not captured in the kit. |
| E4 | Sampled control-plane and data-plane host/port coordinates are identical. | Edge routing reads a different assignment than the control plane wrote. |
| E5 | Declared endpoint role equals the role observed from the service, independent of node numbering. | Failover changed the real primary/reader role. |
| E6 | A parameter name promising `reader` or `primary` contains an endpoint with that observed role. | The parameter name and value have contradictory semantics. |
| E7 | Fail-closed exception literals used by server-error paths are ASCII-only. | Error reporting can fail again and hide the original cause. |
| E8 | Every host-table scan excludes synthetic identifiers. | Singleton control rows can be treated as fleet hosts. |
| E9 | Read-only residue observations report no test tenant, VM directory, process, host-file injection, or tunnel. | Test state remains in the target environment. |
| E10 | Known false-red shapes are explicitly annotated and never used alone as rollback evidence. | No failure verdict is produced by this annotation check. |

## C1 limitations

C1 reports missing authorization only. It deliberately does not report
over-grant, so a role manually widened beyond source needs is outside this
tool's scope. Simulation is action-level: an allowed action does not prove that
the role is allowed on the exact resource the code will access.

## E10 known false reds

The validator actively labels these shapes so they are not treated as standalone
rollback reasons:

1. `apply-api-routes verify` can report a CORS fatal because service-added
   default keys are compared as drift.
2. `oc-consistency` can report several drift rows when an intentional
   out-of-band overlay is active.
3. The A-lt `! grep -q "{{"` gate also matches placeholders in comments.
4. `git show $PATCH_SHA:` fails when the environment variable is absent even if
   the commit exists.
5. `grep -c` exits nonzero for a valid zero-match result unless explicitly
   neutralized.

An E10 annotation must be correlated with independent byte, configuration, or
runtime evidence before deciding to roll back.

## Recorded `--offline` baselines

**Why this section exists.** A first-time operator runs `--offline` against a
shipped kit, sees seven `FAIL` rows, and has no way to tell "this kit has always
reported that" from "I broke something". Without a baseline the honest reaction is
to stop and escalate, which is exactly the delay this tool was meant to remove.
The two tables below are what the shipped kits actually report, so a run can be
compared instead of interpreted.

**Scope and limits.** These were recorded with `--offline` only, against
`--gateway-ref origin/gateway`, with no `--environment-json` and no AWS
credentials. `--offline` supplies no live readings, so `A1`, `D5` and `E2` have no
discriminating power here — they are recorded for completeness, not as evidence.
Re-record after any publish that changes a kit; a baseline is only meaningful
against a stated gateway ref.

Recorded against gateway `88c8c42d` (`patch/lc2-patch` at `patch_sha`
`979a9cb7`, `patch/lifecycle-op-patch` at `patch_sha` `c9fd494f`).

| ID | `lifecycle-op-patch` | `lc2-patch` |
| --- | --- | --- |
| A1 | FAIL | UNVERIFIED |
| D1 | PASS | PASS |
| D2 | PASS | PASS |
| D3 | FAIL | PASS |
| D4 | PASS | PASS |
| D5 | FAIL | UNVERIFIED |
| D6 | FAIL | PASS |
| D7 | FAIL | FAIL |
| E2 | INCONCLUSIVE | INCONCLUSIVE |
| E7 | FAIL | FAIL |
| E8 | FAIL | FAIL |
| E10 | PASS | PASS |

Both kits exit `1` in this mode, because exit `1` means "at least one FAIL" and
says nothing about whether the FAIL is new.

### How to use it

Run the tool, then diff your rows against the column for your kit.

- **Every row matches** — the kit is in its shipped state. Proceed with APPLY.
  Do not spend time on the FAIL rows below; they are explained here.
- **Any row is worse than the baseline** (PASS→FAIL, or a new id appears) — stop.
  That delta is about your tree or your invocation, not about the kit.
- **Any row is better than the baseline** — also worth a look, usually it means
  you pointed `--gateway-ref` at a different ref than the one recorded above.

### Why each non-PASS row is expected

Rows shared by both kits are properties of the source tree, not of kit packaging,
so no amount of repackaging moves them:

- **D7** — `lib/apply-cfn-resources.sh` uses `mapfile`, which is bash 4+. The
  rule is correct, but the script already guards it: it checks `BASH_VERSION` up
  front and aborts with `FATAL: bash <version> is too old; this needs bash 4+
  (mapfile).` So on macOS (bash 3.2) the operator gets a named cause rather than
  `mapfile: command not found` at exit 127. Run that script under bash 4+.
- **E7** — the fail-closed exception literals in
  `deploy/lambda/api/core/create_deadline.py` and `core/deadline_config.py` are
  Chinese, so they are not ASCII-safe. Note this is the *source* of the message;
  the reporting *transport* that used to fail on those bytes was fixed under
  `#655`, which is in both kits.
- **E8** — the rule wants every host-table scan to exclude synthetic ids. Some
  call sites already do (`services/fleet_service.py` matches the exclusion
  shape); others in the same group do not (`core/scheduling.py` does not), and the
  group is reported as one row.

Rows that are FAIL only on `lifecycle-op-patch` reflect that it is the older kit
in the chain, not a defect introduced by it:

- **D3** — the check pairs the newest publish marker with the manifest log by
  filename slug. That kit's `patch_sha` is `c9fd494f`, and the markers under
  `patch/` have since rotated well past it (newest slug is `3c4494e8`), so no
  marker names its commit any more.
- **D6** — its S3 writes predate the convention of reading and recording the
  previous `VersionId` as a rollback anchor.
- **A1** and **D5** are `FAIL` here and `UNVERIFIED` on `lc2-patch`: this kit
  declares critical files whose bytes the offline comparison can resolve on one
  side only, and three `B-s3` scripts (`launch-vm.sh`, `stop-vm.sh`,
  `migrate-vm.sh`) carry no `mode` in the manifest. Neither is decidable without
  live readings — see the scope note above.