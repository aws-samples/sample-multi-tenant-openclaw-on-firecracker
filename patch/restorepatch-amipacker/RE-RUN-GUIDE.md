# Re-running this kit on an environment that already applied it

Read this instead of starting `APPLY-INSTRUCTIONS.md` from the top when **any earlier revision of
`restorepatch-amipacker` has already been applied to the target**. The steps are the same; the
preconditions, the expected output and the failure modes are not. `APPLY-INSTRUCTIONS.md` is
written as a first install, and following it verbatim on a second run is how an operator ends up
treating a correct `ALREADY` as a problem — or worse, forcing past a real one.

The kit baseline is unchanged across revisions (the published `428patch` tag), so you still apply
exactly **one** kit. You are not stacking patches.

## 1. Decide which case you are in

Run the read-only reconcile first. It touches nothing.

```bash
bash lib/apply-restorepatch.sh reconcile --env environment.json --kit . --scope all
```

| What you see | Case | What it means |
|---|---|---|
| Most places `PATCH`, a few `UNKNOWN` | **A — re-run of a NEWER revision** | The environment carries an earlier revision. `UNKNOWN` is expected: see §4. |
| Everything `PATCH`, verdict clean | **B — re-run of the SAME revision** | Nothing to do except the read-only gates. Expect `ALREADY` everywhere. |
| Many places `BASE` | **C — not actually applied yet** | Stop reading this file and use `APPLY-INSTRUCTIONS.md`. |

A `verdict=DRIFTED` line on its own does **not** mean the environment is broken. Read the counters:
`absent` and `unreadable` are the ones that demand attention. `unknown` on host scripts is normal
on an environment whose control plane was deployed by CDK rather than by this kit (§4).

## 2. What is genuinely idempotent, with the evidence

Verified on REST API `sa617zh9eb` stage `v1` (us-east-2), on two environment shapes:

| Environment shape | First apply | Repeat apply | Stage deployment |
|---|---|---|---|
| Already carried the earlier revision's routes | exit 0 — existing routes report `ALREADY … skipping`, only the new ones are created | exit 0, `ALREADY` | unchanged |
| Carried none of them | exit 0 — all routes created | exit 0, `ALREADY` | unchanged, route-tree digest identical |

So a second `apply` of the route step converges rather than failing, and it does **not** create a
second deployment or repoint the stage again. The same holds for the S3 objects (byte-identical
uploads are skipped and the previous VersionId is recorded) and for the Lambda code overlay
(each module is compared by sha before it is written).

## 3. The command sequence for a re-run

```bash
# 0. authenticity — always, it is free and it catches a truncated download
#    (the Step 0.0 block in APPLY-INSTRUCTIONS.md)

# 1. read-only: where does this environment actually stand
bash lib/discover-env.sh <region> manifest.json
bash lib/apply-restorepatch.sh reconcile --env environment.json --kit . --scope all

# 2. recovery anchors, even on a re-run — a re-run can still change Lambda code
bash lib/apply-restorepatch.sh backup   --env environment.json --kit .

# 3. control plane and data plane
bash lib/apply-restorepatch.sh apply    --env environment.json --kit . [--values render-values.json]

# 4. routes — non-interactive callers MUST pass --yes, see §5
bash lib/apply-api-routes.sh apply    lib/api-routes.spec.json "$API_ID" v1 "$REGION" --yes
bash lib/apply-api-routes.sh verify   lib/api-routes.spec.json "$API_ID" v1 "$REGION"
bash lib/apply-api-routes.sh finalize  lib/api-routes.spec.json "$API_ID" v1 "$REGION" --yes

# 5. acceptance
bash lib/apply-restorepatch.sh verify     --env environment.json --kit . --scope all
bash lib/apply-restorepatch.sh reconcile  --env environment.json --kit . --scope all
```

`canary` and `refresh` are only needed when the bootstrap/launch-template actually changed. If
`apply` reports `bootstrap=ALREADY`, the fleet is already on the target launch template and
replacing instances buys you nothing — skip both and go straight to acceptance.

## 4. Why an already-patched environment still shows `UNKNOWN`

Each artifact declares only two digests: `base_sha256` (the pristine tree) and `patch_sha256`
(this revision). An environment that applied an **earlier** revision holds content that is
neither — so it is classified `UNKNOWN`, and `apply` will ask for `--allow-base-drift` before it
overwrites it.

That flag was designed as an escape hatch for unknown provenance, and on a normal upgrade path it
becomes mandatory. Before you pass it:

- Confirm the `UNKNOWN` digests match the **previous revision's** `patch_sha256` (keep the old
  manifest, or diff against the previous kit). Then it is this kit's own earlier output, not a
  hand edit, and overwriting it is correct.
- If a digest matches neither revision, someone changed that file by hand. Find out what and why
  before you overwrite it.

`init-host.sh` is a special case and should NOT show `UNKNOWN` any more: it is a template, so the
in-service object is a rendered product whose bytes can never equal the template digest. Reconcile
now judges it by re-rendering the template against the in-service object and comparing code lines,
and it looks it up under the prefix the launch template actually references. If it still reports
`UNKNOWN`, the in-service bootstrap really does differ.

## 5. Things a re-run operator gets wrong

- **Non-interactive route apply.** `apply`, `finalize` and `rollback` gate on a typed confirmation.
  Without a TTY the prompt hits EOF. Pass `--yes` (the run then logs
  `gate approved non-interactively via --yes`), or pipe `printf 'APPLY\n' |`. A plain invocation in
  CI aborts.
- **A new placeholder in the bootstrap template.** A newer revision can add one that the
  in-service script predates, so none of the three recovery tiers can supply it. Supply it with
  `--values`; only `IMMUTABLE_DISK_REQUIRED` has a declared compatibility default (`false`,
  matching the CDK default) and the run prints when it falls back.
- **Reading `verdict=DRIFTED` as failure.** See §1.
- **Skipping `backup` because "nothing will change".** A re-run still publishes Lambda code if any
  module differs, and the alias still moves. Take the anchors.
- **Re-running `backup` after a previous run already published a version.** The recovery anchor is
  recorded from what is live *now*, so a second `backup` records the already-patched version as the
  rollback target. If you need a pre-patch anchor, capture it before the first run, or keep the
  original package from that run. This is a real gap, not a theoretical one.

## 6. What re-running does NOT fix

Running this kit again will not move any of these. Plan them separately.

- **Lambda environment variables.** The driver only calls `update-function-code`. Any fix that
  needs a new env key stays inactive, and the verify assertion on env-key count passing does not
  mean the key arrived.
- **Boot-time files on already-running hosts.** `deployment/scripts/*` reach a host at boot, via a
  control-plane self-heal path, or by a manual push. A host that booted before the upload keeps the
  old file until it is replaced. Re-running the kit does not push to the live fleet.
- **`provision-host.sh`.** Delivered only by baking an AMI. It will read `BASE` on every host until
  the image is rebuilt, and that is expected.
- **Edge-node assets.** The edge fixes are repository files applied by a CDK deployment. This kit
  publishes only `edge/fluent-bit/**` to S3, because that is what a host pulls. On an environment
  with an HA edge, the remaining edge fixes are not delivered by this kit.
- **Anything the target's topology does not have.** A missing peer function is reported
  `NOT_APPLICABLE` / `ABSENT; no deployed target` rather than as drift, and its fixes simply do not
  land.

## 7. Before you call the re-run done

- `verify --scope all` exits 0 with no `FAIL` lines. The invoke probe now sends a proxy-shaped
  event and treats a Lambda `FunctionError` as a failure, so a green line here means the entry
  point really answered — it is no longer a note you can ignore.
- The final `reconcile` has `absent=0` and `unreadable=0`.
- `post-verify` and `assert-routes-present` actually ran. If the run stopped at `final-reconcile`,
  they did not, and you have no real-entry-point evidence.
- The stage's active deployment serves every declared route (`verify` prints this).
