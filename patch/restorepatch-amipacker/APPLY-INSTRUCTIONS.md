# restorepatch-amipacker hot-apply instructions

This kit applies the control-plane source overlay and prepares the durable host
replacement path without updating the existing stack. Run every AWS command with
an explicit region.

The driver subcommands are `precheck`, `backup`, `apply`, `apply-control`,
`canary`, `refresh`, `verify`, `rollback`, `apply-api`, `verify-api`,
`finalize-api`, and `rollback-api`. `--values <file>` optionally supplies a JSON
object keyed by bootstrap placeholder name when a value cannot be recovered from
the live rendered script. `verify` accepts an optional
`--scope <control|data|routes|all>`; omitting it keeps the existing `all`
behavior.

For a full rollout, use this order:
`precheck → backup → apply → canary → refresh → verify`. A failed or missing
canary gate forbids `refresh`.

## Step 0.0 Authenticity check

Every shipped artifact must have the same SHA-256 value as its
`manifest.paths[path].patch_sha256`.

```bash
python3 - <<'PY'
import hashlib
import json
import pathlib

kit = pathlib.Path(".")
manifest = json.loads((kit / "manifest.json").read_text())
bad = []
for source, record in manifest["paths"].items():
    artifact = record.get("artifact")
    if not artifact:
        continue
    actual = hashlib.sha256((kit / artifact).read_bytes()).hexdigest()
    if actual != record["patch_sha256"]:
        bad.append((source, artifact, record["patch_sha256"], actual))
if bad:
    for row in bad:
        print("MISMATCH", *row)
    raise SystemExit("STOP: artifact SHA-256 mismatch")
print("PASS: all shipped artifacts match manifest SHA-256 values")
PY
```

## Step 0 Read-only discovery

Discovery confirms the control-plane API only from the configured client URL plus
successful authenticated probes of every required route. A route or name match is
never enough. Supply the client call site before running it:

- `OC_CONTROL_PLANE_URL` is required. Without it the API stays unresolved and every
  later concern that correlates through the serving Lambda stays unresolved too.
- `OC_CONTROL_PLANE_PROBE_HEADERS_FILE` points at a JSON object of request headers
  (for example an `x-api-key` entry) when the API requires authentication. Values
  must be plain strings. Keep the file out of version control.
- `OC_CONTROL_PLANE_API_ID` is required only for a custom domain, where the API id
  cannot be derived from the host name.
- `OC_CONTROL_PLANE_PROBE_PATHS` overrides the default `/tenants,/hosts` probe set.
- Run discovery from a place that can actually reach that URL. On the private API
  topology the probes only succeed from inside the VPC, so run the kit on a host in
  the VPC; from a workstation the probes return `000` and the API stays unresolved.

```bash
export OC_CONTROL_PLANE_URL="https://<api-id>.execute-api.${REGION}.amazonaws.com/<stage>"
export OC_CONTROL_PLANE_PROBE_HEADERS_FILE=/path/to/headers.json
bash lib/discover-env.sh "${REGION}" manifest.json
```

The command writes `environment.json`. Continue only when the API, host ASG, and
asset bucket are machine-confirmed. Then run:

```bash
bash lib/apply-restorepatch.sh precheck \
  --env environment.json --kit .
```

`precheck` reports `ALREADY`, `READY`, or `DRIFT` independently for the lifecycle
hook, bootstrap, and Lambda overlay. A fully applied environment exits successfully
because a rerun is a no-op.

## Unattended path (answer once, then apply)

Print the complete derived interview, then fill one JSON answers file from the
skeleton at the end of the output:

```bash
python3 lib/interview-once.py ask .
${EDITOR:-vi} answers.json
bash lib/autopatch.sh . --answers answers.json --region "${REGION}" --yes
```

Every human decision is recorded before the first target write. After the plan
echo, the driver proceeds through the selected phases without another prompt and
stops only on a machine gate or failed command. It passes and records the scope
applied for each verify step, so a control-plane-only run does not report skipped
data-plane concerns as failures. When the auth answer is
`resolve-from-usage-plan`, the key value is written only to a mode-0600 temporary
headers file that is removed when the run exits; it is never written to
`DECISION.json`, the receipt, or logs. The numbered per-phase steps below remain
the supported manual path for operators who want to drive each phase separately.

## Step 1 Evidence and impact assessment

Record the current Lambda digest, environment-key count, ASG capacity, lifecycle
timeout, LT version, AMI, bootstrap prefixes, tenant count, and host-ledger count.

```bash
aws lambda get-function --function-name openclaw-api --region "${REGION}" \
  --query 'Configuration.[CodeSha256,RevisionId]'
aws lambda get-function-configuration --function-name openclaw-api \
  --region "${REGION}" --query 'length(Environment.Variables)'
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "${HOST_ASG}" --region "${REGION}" \
  --query 'AutoScalingGroups[0].[MinSize,MaxSize,DesiredCapacity,length(Instances)]'
aws autoscaling describe-lifecycle-hooks \
  --auto-scaling-group-name "${HOST_ASG}" --region "${REGION}"
```

Create the mandatory recovery record before apply:

```bash
bash lib/apply-restorepatch.sh backup \
  --env environment.json --kit .
```

The recovery state is stored inside the kit. Rollback refuses to run without it.
`backup` is not read-only: it calls Lambda `publish-version` to create the
pre-restorepatch recovery anchor before recording that version.

## Step 2 Control-plane overlay

This is the patch's only immediate hot repair. The driver downloads each live
Lambda package, overlays every shipped module including newly added modules, keeps
the live dependencies, updates the unqualified function, publishes a version, and
advances the runtime-discovered alias when one exists.

The API overlay scope is every deployed function that carries the API package:
the API function plus every peer confirmed by discovery. This includes
`openclaw-lifecycle-consumer`, whose asynchronous lifecycle actions would
otherwise continue running the original package after the API function was
updated. Confirm the complete read-only scope from `environment.json` before
applying:

```bash
jq -r '
  .lambda_link
  | "confirmed=\(.peer_discovery_confirmed)",
    "function=\(.function) esm_qualifier=\(.esm_qualifier // "")",
    ((.peers // [])[] | select(.probe_paths_present == true)
      | "peer=\(.function) esm_qualifier=\(.esm_qualifier)")
' environment.json
```

Version publication and alias advancement remain scoped to the API function;
discovered peers consume the unqualified `$LATEST` code.

```bash
bash lib/apply-restorepatch.sh apply-control \
  --env environment.json --kit .
```

`apply-control` requires only the confirmed region and Lambda function. It does
not read or mutate the ASG, launch template, assets bucket, or AMI, so the
control-plane patch can be delivered before a replacement AMI is available.

The API overlay is verified on both execution paths: the unqualified function and
the alias-resolved published version must report the same `CodeSha256`.

## Step 3 Packer AMI and gated LT replacement

Follow `host-scripts/packer/CUSTOMER-GUIDE.md`. In section 3, use option B, the
manual upload procedure. The image synchronization script referenced by option A
lives under an unpublished engineering path and is not available here.

Create `host-scripts/packer/my.pkrvars.hcl` from the provided regional variables,
fill the customer account, VPC, subnet, bucket, and instance profile, then keep it
out of version control. The public `.gitignore` does not exclude that file.

```bash
packer init host-scripts/packer/host-golden.pkr.hcl
packer build -var-file=host-scripts/packer/my.pkrvars.hcl \
  host-scripts/packer/host-golden.pkr.hcl
bash host-scripts/packer/assert-parity.sh
```

Set `new_ami_id` in `environment.json`, rerun backup, then apply. The driver
recovers placeholders from assignments, matching whole code lines, and anchored
script blocks in the live rendered bootstrap. `--values` may be omitted when
those tiers resolve every value; otherwise provide the remaining values:

```bash
bash lib/apply-restorepatch.sh apply \
  --env environment.json --kit . --values render-values.json
```

If bootstrap state is `DRIFT`, the command refuses that bootstrap mutation and
publishes no host assets in that run, preventing a mixed-lineage fleet. It continues
the other concerns. After reviewing the in-service content, the explicit override is:

```bash
bash lib/apply-restorepatch.sh apply \
  --env environment.json --kit . --values render-values.json --allow-base-drift
```

The override extracts every in-service
`deployment/bootstrap/host/` 64-hex prefix, replaces all of them, and refuses to
create an LT version unless the target is present, all old prefixes are absent,
and the rendered bootstrap has no unresolved template marker on a code line.
Historical `{{AVAIL_VCPU}}/{{AVAIL_MEM}}` markers in comments are preserved.

When the bootstrap gate permits publication, the driver also publishes
`launch-vm.sh`, `rebuild-vm.sh`, `reset-vm.sh`, the Fluent Bit files, and finally
`host-agent.py`. It records the prior S3 object versions and verifies each uploaded
byte length. The host-agent unit is delivered inline: `{{HOST_AGENT_SCRIPT}}` is
rendered as a `SVCEOF` heredoc containing the raw contents of
`host-scripts/host-agent.service.patched`. `apply` creates and promotes the LT
version but deliberately does not start a fleet refresh.

Run the single-host canary next:

```bash
bash lib/apply-restorepatch.sh canary \
  --env environment.json --kit .
```

The canary first confirms that `MaxSize` has one free slot, then temporarily raises
desired capacity by one without changing `MinSize`. It waits for one new host on the
promoted LT and checks cloud-init/bootstrap logs, the active host-agent service, all
four host scripts, unresolved markers, `PYTHONUNBUFFERED=1`, and the unit's
`OC_AGENT_PORT`. After PASS or any failure where the canary instance is known, the
driver removes that exact instance with
`terminate-instance-in-auto-scaling-group --should-decrement-desired-capacity`; it
does not use termination-policy-controlled scale-in. Desired capacity is then
asserted to equal its original value. Do not continue unless this phase prints
`canary PASS`.

Only after a passing canary, start the launch-before-terminate refresh:

```bash
bash lib/apply-restorepatch.sh refresh \
  --env environment.json --kit .
```

The refresh uses both `MinHealthyPercentage=100` and
`MaxHealthyPercentage=100`, which is the AWS-documented combination for replacing
one instance at a time by launching the replacement before terminating the old
instance.

## Step 3b Optional stopgap

Hosts are commonly in private subnets, so SSH may be unavailable. An urgent host
file replacement can be sent through SSM, but it is only a stopgap and disappears
when the instance is replaced.

Use base64 as one line or provide `--cli-input-json`. Do not pass a multiline
script through the `commands=[]` shorthand: line breaks can become literal `n`
characters while SSM still reports `Success`.

```bash
B64="$(base64 < host-scripts/reset-vm.sh.patched | tr -d '\n')"
PAYLOAD="$(printf '{"Parameters":{"commands":["echo %s | base64 -d > /home/ubuntu/reset-vm.sh && chmod 0644 /home/ubuntu/reset-vm.sh"]}}' "$B64")"
aws ssm send-command --instance-ids "${CANARY_INSTANCE_ID}" \
  --document-name AWS-RunShellScript --cli-input-json "$PAYLOAD" \
  --region "${REGION}"
```

When inspecting a large Lambda archive under `set -o pipefail`, first store
`unzip -l` output in a variable and then search it. A direct
`unzip -l big.zip | grep -q` pipeline can terminate unzip with status 141 and
produce a false failure.

## Step 4 Optional resource configuration

`InitHook.HeartbeatTimeout` may be widened from 1200 to 3600 seconds after the
current value is recorded. `HostASG.MinSize` is deliberately not changed.

The zero minimum is first-deploy guidance. Applying it to a live fleet permits the
group to scale to zero and can remove hosts carrying real tenants.

The #423 private API topology requires manual review. Before creating an
`execute-api` interface endpoint, query the target VPC for an existing endpoint
with private DNS enabled. AWS permits only one such endpoint for the service in a
VPC. Stop on a conflict instead of attempting the topology change.

The six LiteLLM removals are guarded by describe calls. This customer configuration
uses an external gateway, so those resources are expected to be absent. If any are
present, stop and investigate.

## Step 5 Deployment-machine file replacement

Copy the contents of `host-scripts/deploy-machine/` over the matching
repository-relative paths on the deployment machine. This includes stack source,
configuration, build, preflight, and CodeBuild inputs. It does not mutate running
AWS resources.

The static `console/` deletion is repository convergence only. Customer console
delivery has moved to the `openclaw-console-bff` Lambda, so no customer-side file
deletion is required.

## Step 6 New-host verification

After the refresh has completed, run:

```bash
bash lib/apply-restorepatch.sh verify \
  --env environment.json --kit .
```

The command above keeps the default `all` scope. Use `--scope control` or `data`
when manually verifying only that concern. Route assertions remain under
`verify-api`, which also accepts `--scope routes`.

A replacement host passes only when all three signals are present:

1. It is `Healthy` and `InService` on the newly created LT version.
2. Its exact instance id is present in the scheduler host ledger.
3. SSM is online, cloud-init completed, `host-agent` is active, the staged script
   has no unresolved marker, and its SHA-256 matches the published object.

For Lambda invocation, absence of `FunctionError` is the hard signal. A private
API `/ping` body may be 404 and is not by itself a failure.

Report probe failures as `FAIL`, `INCONCLUSIVE`, or `ABSENT`; never turn missing
evidence into a pass.

## Step 7 Per-fix verification plan

Run every record in `manifest.verifications`.

Phase A is read-only and checks authorization, response contracts, query fields,
idempotency records, environment-key preservation, alias convergence, host counters,
and deployment-machine preflight behavior.

Phase B exercises a real lifecycle: Packer parity, a controlled host replacement
including tenant evacuation, the one-shot restore key and the termination window
held open across every tenant on the host, backup source reporting, device
pairing self-heal, Fluent Bit activity, host-agent logging, rebuild/reset
fencing, capacity accounting, and private API routing. Phase A additionally
checks that a tenant reaching its deleted state carries no stale health fields. A tenant merely reaching `running` is not sufficient;
capture the issue-specific observable and prove the old failure signal is absent.

## Step 8 Exact cleanup

Record every test tenant id at creation time. Delete those exact ids one by one,
wait for each deletion to finish, and confirm its exact data directory and process
are absent. Never use a prefix glob. The final real-tenant and host counts must
match the Step 1 evidence.

Rollback uses the recorded state:

```bash
bash lib/apply-restorepatch.sh rollback \
  --env environment.json --kit .
```

Rollback restores the unqualified API code and the discovered alias path because
lifecycle dispatch uses the unqualified function while API methods may use the
alias.

## Known limitations

The upstream manifest schema currently restricts `resource_type` to `^AWS::`.
The captured closure legitimately includes `Custom::CDKBucketDeployment` and
`Custom::AWS`; validation therefore requires that pattern to accept
`^(AWS|Custom)::`.

The resource types cannot be renamed to bypass the schema. The closure key
function includes `resource_type`; changing it would make the captured resource
change appear unowned.
