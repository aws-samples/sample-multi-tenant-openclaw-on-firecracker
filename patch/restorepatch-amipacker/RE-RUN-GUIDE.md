# Re-running this kit on an environment that already applied it

Read this instead of starting `APPLY-INSTRUCTIONS.md` from the top when **any earlier revision of
`restorepatch-amipacker` has already been applied to the target**. The steps are the same; the
preconditions, the expected output and the failure modes are not. `APPLY-INSTRUCTIONS.md` is
written as a first install, and following it verbatim on a second run is how an operator ends up
treating a correct `ALREADY` as a problem — or worse, forcing past a real one.

The kit baseline is unchanged across revisions (the published `428patch` tag), so you still apply
exactly **one** kit. You are not stacking patches.

Everything in §1, §5 and §8 below was learned from real re-runs, including one that reached the
point of nearly performing a harmful manual edit. Read §5 before you touch anything.

## 1. Decide which case you are in, and how to read the counters

Run the read-only reconcile first. It touches nothing.

```bash
bash lib/apply-restorepatch.sh reconcile --env environment.json --kit . --scope all
```

| What you see | Case | What it means |
|---|---|---|
| Most places `PATCH`, a few `UNKNOWN` | **A — re-run of a NEWER revision** | The environment carries an earlier revision. `UNKNOWN` is expected: see §4. |
| Everything `PATCH`, verdict clean | **B — re-run of the SAME revision** | Nothing to do except the read-only gates. Expect `ALREADY` everywhere. |
| Many places `BASE` | **C — not actually applied yet** | Stop reading this file and use `APPLY-INSTRUCTIONS.md`. |

`verdict=DRIFTED` on its own does **not** mean the environment is broken, and reconcile exits
non-zero whenever the verdict is not clean. Read the counters instead:

| Counter | Before apply | After apply | Notes |
|---|---|---|---|
| `patch` | any | should grow | in-service content equals this revision |
| `base` | any | may remain | equals the pristine tree, i.e. a file the previous revision never touched. Normal; apply overwrites it. |
| `unknown` | expected, often large | expected | neither pristine nor this revision — usually the previous revision's own output, or a CDK-deployed product tree. See §4. |
| `absent` | **expected to be non-zero** | **must be 0** | see the rule below |
| `unreadable` | **must be 0** | **must be 0** | never a content verdict — it means the check could not run. See §5. |
| `split` | must be 0 | must be 0 | S3 and the hosts disagree with each other |

**The `absent` rule.** A path this revision ADDS cannot exist in the target before you apply it,
so `ABSENT` is the correct reading, not a failure. Check the manifest before you stop:

```bash
grep 'state=ABSENT' <your-reconcile-output> | sed 's/.*source=/source=/' | sort -u
python3 - <<'PY'
import json
m = json.load(open("manifest.json"))
for p in ["deploy/lambda/api/core/host_taint.py"]:      # paste the source= paths here
    print(p, m["paths"][p]["change"])
PY
```

`change=A` → added by this revision, `ABSENT` pre-apply is correct, keep going.
`change=M` but `ABSENT` → that is a genuine missing file, stop.

Note the counter is per PLACE, not per file: a C-lambda module is checked once per deployed
function, so three added modules across two functions report `absent=6`. Measured on a real
upgrade: `absent=9` decomposed into 6 × three added modules across two functions, plus
3 × `provision-host.sh` on three hosts (AMI-only delivery, §6). All nine were expected.

## 2. What is genuinely idempotent, with the evidence

| Layer | Second run | Evidence |
|---|---|---|
| API routes | `ALREADY`, no new deployment, stage unchanged | verified on two environment shapes, below |
| Lambda code overlay | package bytes identical, so no new version and the alias does not move | package mtimes are normalized before zipping; verified on a real 8,064,403-byte package: two consecutive overlays produced byte-identical zips |
| S3 host scripts | byte-identical uploads are skipped and the previous VersionId is recorded | per-object sha comparison before write |

Route apply, measured on one REST API and stage, two environment shapes:

| Environment shape | First apply | Repeat apply | Stage deployment |
|---|---|---|---|
| Already carried the earlier revision's routes | exit 0 — existing routes report `ALREADY … skipping`, only the new ones are created | exit 0, `ALREADY` | unchanged |
| Carried none of them | exit 0 — all routes created | exit 0, `ALREADY` | unchanged, route-tree digest identical |

**Why the Lambda side needed a fix to be idempotent at all.** Lambda derives `CodeSha256` from the
zip BYTES. The overlay copies files into a copy of the live package, which stamps them with the
current time, so re-applying byte-identical code still produced a different zip: measured on a
real function, two consecutive `apply-control` runs produced zips of the same size with **zero**
content differences and 21 differing mtimes — yet `CodeSha256` changed, a new version was
published, and the alias advanced. That is what silently invalidated the recorded rollback anchor
between runs. Mtimes are now normalized to the zip epoch floor before packing (the same reason CDK
normalizes), so an unchanged re-run no longer publishes a version or moves the alias.

If you are re-running against an environment that was last patched by a kit revision from **before**
this fix, expect exactly ONE more version publish on the first run after the upgrade — that run
migrates the package to normalized mtimes. Runs after it are stable.

## 3. The command sequence for a re-run

```bash
# 0. authenticity — always, it is free and it catches a truncated download
#    (the Step 0.0 block in APPLY-INSTRUCTIONS.md)

# 1. read-only: where does this environment actually stand
bash lib/discover-env.sh <region> manifest.json        # see §8 if the API is PRIVATE
bash lib/apply-restorepatch.sh reconcile --env environment.json --kit . --scope all

# 2. recovery anchors — take them BEFORE the first apply, see §5
bash lib/apply-restorepatch.sh backup   --env environment.json --kit .

# 3a. control plane only — no AMI required
bash lib/apply-restorepatch.sh apply-control --env environment.json --kit .

# 3b. OR the full apply — control plane + data plane + launch template.
#     Requires a freshly baked AMI: without `new_ami_id` in environment.json this fails
#     closed with "bake the AMI per host-scripts/packer/CUSTOMER-GUIDE.md first".
bash lib/apply-restorepatch.sh apply    --env environment.json --kit . [--values render-values.json]

# 4. routes — non-interactive callers MUST pass --yes, see §5
bash lib/apply-api-routes.sh apply     lib/api-routes.spec.json "$API_ID" v1 "$REGION" --yes
bash lib/apply-api-routes.sh verify    lib/api-routes.spec.json "$API_ID" v1 "$REGION"
bash lib/apply-api-routes.sh finalize  lib/api-routes.spec.json "$API_ID" v1 "$REGION" --yes

# 5. acceptance
bash lib/apply-restorepatch.sh verify     --env environment.json --kit . --scope all
bash lib/apply-restorepatch.sh reconcile  --env environment.json --kit . --scope all
```

**Choosing 3a or 3b is not a matter of taste.** If the control plane goes to this revision while
the data plane stays on the old one, the new control plane calls host scripts that are not there
yet — that combination has already broken a production environment once and had to be rescued by
copying scripts onto live hosts by hand. So either do both, or do neither. If you cannot complete
the data plane in this session (no AMI, no S3 write access — see §5), do **not** run 3a as a
consolation prize; leave the environment alone and come back when you can do both.

`canary` and `refresh` are only needed when the bootstrap/launch-template actually changed. If
`apply` reports `bootstrap=ALREADY`, the fleet is already on the target launch template and
replacing instances buys you nothing — skip both and go straight to acceptance.

## 4. Why an already-patched environment still shows `UNKNOWN`

Each artifact declares only two digests: `base_sha256` (the pristine tree) and `patch_sha256`
(this revision). An environment that applied an **earlier** revision holds content that is
neither — so it is classified `UNKNOWN`, and `apply` will ask for `--allow-base-drift` before it
overwrites it. A control plane deployed by CDK rather than by this kit lands in the same bucket:
its content is the product tree, which is neither digest.

That flag was designed as an escape hatch for unknown provenance, and on a normal upgrade path it
becomes mandatory. Before you pass it:

- Confirm the `UNKNOWN` digests match the **previous revision's** `patch_sha256` (keep the old
  manifest, or diff against the previous kit). Then it is this kit's own earlier output, not a
  hand edit, and overwriting it is correct.
- If a digest matches neither revision, someone changed that file by hand. Find out what and why
  before you overwrite it.
- `--allow-base-drift` relaxes a WRITE-side precondition. It has nothing to do with `unreadable`,
  which is a read failure — never reach for it to make `unreadable` go away.

`init-host.sh` is a special case and should NOT show `UNKNOWN` any more: it is a template, so the
in-service object is a rendered product whose bytes can never equal the template digest. Reconcile
now judges it by re-rendering the template against the in-service object and comparing code lines,
and it looks it up under the prefix the launch template actually references. If it still reports
`UNKNOWN`, the in-service bootstrap really does differ.

## 5. Things a re-run operator gets wrong

- **Editing `core/envelope.py` to "remove the #479 rejection".** Do not. An earlier revision's
  rejection of plaintext sensitive fields under `scheme=asymmetric-v1` did break production
  `create-tenant`, and the emergency fix at the time was to strip that block by hand. It has since
  been fixed upstream properly: the rejection is now exempted for the fields in
  `_PLAINTEXT_COMPAT_FIELDS` (`llm_key`) when `param_class` is `config`, with the audit WARNING
  retained, and everything outside that list still rejected. Grepping only for the rejection line
  and not its guard leads straight to re-stripping code that is already correct — which would
  delete the exemption itself and leave the environment worse than before. Apply as shipped.
- **Non-interactive route apply.** `apply`, `finalize` and `rollback` gate on a typed confirmation.
  Without a TTY the prompt hits EOF and the run ends in a traceback. Pass `--yes` (the run then
  logs `gate approved non-interactively via --yes`), or pipe `printf 'APPLY\n' |`.
- **A stale lease after an interrupted run.** `apply`/`finalize`/`rollback` hold
  `~/.oc-apply-api-routes/<api-id>.<stage>.json.lock`. If the process is killed, the lock file
  survives and the next run refuses with `another apply/rollback holds the lease`. Confirm no
  process is actually running (`pgrep -f apply-api-routes`), then move the `.lock` aside. Do **not**
  touch the sibling `<api-id>.<stage>.json` — that is the apply state and finalize/rollback need it.
- **Treating `unreadable` as drift.** It means the check could not run. Distinguish permission from
  absence before deciding, because they read the same but are handled oppositely:
  `aws s3api head-object --bucket <assets-bucket> --key deployment/scripts/host-agent.py` →
  `AccessDenied` is permission, `404`/`NoSuchKey` is a genuinely missing object. `ListBucket` being
  denied does not imply `GetObject` is denied; they are separate permissions, so never infer one
  from the other. The data-plane apply needs `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` and
  `s3:GetObjectVersion` on the assets bucket (it records the overwritten VersionId so a rollback
  is possible). It does not need `s3:CreateBucket`.
- **A new placeholder in the bootstrap template.** A newer revision can add one that the
  in-service script predates, so none of the three recovery tiers can supply it. Supply it with
  `--values`; only `IMMUTABLE_DISK_REQUIRED` has a declared compatibility default (`false`,
  matching the deployment default) and the run prints when it falls back.
- **Reading `verdict=DRIFTED` as failure.** See §1.
- **Skipping `backup` because "nothing will change".** A re-run still publishes Lambda code if any
  module differs, and the alias still moves.
- **Trusting the anchor a SECOND `backup` recorded.** `backup` records what is live *now*, so
  running it again after a previous apply records the already-patched version as the rollback
  target. Capture the pre-patch anchor — the alias's current version number and a copy of that
  version's package — **before the first apply**, and keep it outside the kit directory.

## 6. What re-running does NOT fix

Running this kit again will not move any of these. Plan them separately.

- **Lambda environment variables.** The driver only calls `update-function-code`. Any fix that
  needs a new env key stays inactive, and the verify assertion on env-key count passing does not
  mean the key arrived. Measured: the key count is identical before and after a full overlay.
- **Boot-time files on already-running hosts.** `deployment/scripts/*` reach a host at boot, via a
  control-plane self-heal path, or by a manual push. A host that booted before the upload keeps the
  old file until it is replaced. Re-running the kit does not push to the live fleet.
- **`provision-host.sh`.** Delivered only by baking an AMI. It reads `BASE` on every host until the
  image is rebuilt, and that is expected — do not rebake an AMI just for this counter.
- **Edge-node assets.** The edge fixes are repository files applied by a deployment. This kit
  publishes only `edge/fluent-bit/**` to S3, because that is what a host pulls. On an environment
  with an HA edge, the remaining edge fixes are not delivered by this kit.
- **Anything the target's topology does not have.** A missing peer function is reported
  `NOT_APPLICABLE` / `ABSENT; no deployed target` rather than as drift, and its fixes simply do not
  land.
- **fluent-bit fixes when logging is off.** If the rendered bootstrap carries
  `LOGGING_ENABLED="false"`, the whole installer branch is skipped and the fluent-bit fixes never
  take effect. That is not a failure, but say so explicitly in the report rather than claiming
  those fixes landed. When it is `true`, check four things on a host: the service is active, the
  first line of `/etc/fluent-bit/fluent-bit.conf` names the managed source file (not a package
  default), `add_timestamp.lua` is on disk, and both `Name kinesis_firehose` and a non-empty
  `delivery_stream` are present. The last two are the positive assertions — a distro package
  default satisfies neither, while a mere file-exists check passes on it.

## 7. Before you call the re-run done

- `verify --scope all` exits 0 with no `FAIL` lines. The invoke probe now sends a proxy-shaped
  event and treats a Lambda `FunctionError` as a failure, so a green line here means the entry
  point really answered — it is no longer a note you can ignore. If it does fail, read
  `/tmp/restorepatch-invoke.json`: an `errorType`/`errorMessage` body is a real control-plane
  exception, not a probe artifact.
- The final `reconcile` has `absent=0` and `unreadable=0`.
- `post-verify` and `assert-routes-present` actually ran. If the run stopped at `final-reconcile`,
  they did not, and you have no real-entry-point evidence.
- The stage's active deployment serves every declared route (`verify` prints this).
- A repeat `apply` exits 0, reports `ALREADY`, and leaves the stage deployment, the route tree and
  the Lambda alias unchanged.

## 8. Running discovery when the control plane is a PRIVATE API

Discovery has two halves with opposite requirements: the AWS control-plane calls need
operator-level permissions but work from anywhere, while the authenticated HTTP probes only succeed
from inside the VPC. A host instance role deliberately has the second and not the first, and
`resolve_api_context` fails closed on `control_plane_api.confirmed is not true`, so you cannot skip
the probes.

Two shapes work. Neither requires giving a host any additional AWS permission.

**A. Short-lived credentials into a VPC host (no root on your workstation).** Take a
`sts get-session-token` (1 hour), put the credentials plus the API key into a single SSM
`SecureString`, upload the kit to a prefix the host can already read, then in one SSM command on
the host: fetch the kit, read the parameter, export the three `AWS_*` variables, write the API key
to a `0600` headers file, run `discover-env.sh`, print `environment.json` base64-encoded, and
delete the headers file. Decode it locally and delete the SSM parameter. The host needs only to be
SSM-online and able to read the asset prefix. Verified working end to end.

**B. Port-forward the private endpoint to your workstation (needs root locally).** An SSM
port-forwarding session to the API's own hostname on 443, plus an `/etc/hosts` entry pointing that
hostname at `127.0.0.1` so TLS SNI and the certificate still match. Use local port **443**, not a
high port: discovery treats a URL carrying an explicit port as a custom domain and will leave the
API unresolved. Check that `/etc/hosts` ends with a newline before appending, and remove the entry
afterwards. Port forwarding is a TCP relay, so any SSM-online host in the VPC works — do not pick a
host that carries tenants just because it is "closer" to the fleet.
