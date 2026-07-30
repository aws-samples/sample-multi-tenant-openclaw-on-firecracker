# Claude executor contract: tenant statistics hotfix

Complete this patch through the packaged drivers. Do not rewrite generated kit
files or improvise AWS/CDK commands.

## Stop Before Any Write

Read the customer `config.yml`, but treat `api.mode` only as a hint. Ask the
operator to explicitly confirm all of these:

- the real REST API ID;
- the deployed stage;
- the exact client base URL;
- the JSON file containing the same authentication headers used by the client;
- that this patch must use the explicit `GET /tenants` and `GET /hosts`
  resources, and that every `ANY /{proxy+}` resource is invalid for this patch.

Do not infer or auto-select those values. Discovery must make authenticated
`GET /tenants` and `GET /hosts` calls and both must return 2xx. The selected API
must expose both as exact REST resources. Otherwise stop without generating or
applying a kit.

## Fixed Workflow

1. Export the operator-confirmed values:

   ```bash
   export OC_CONTROL_PLANE_API_ID='<rest-api-id>'
   export OC_CONTROL_PLANE_STAGE='<stage>'
   export OC_CONTROL_PLANE_URL='<https-client-base-url>'
   export OC_CONTROL_PLANE_PROBE_HEADERS_FILE='<absolute-headers-json>'
   export OC_PATCH_HTTP_HEADERS_FILE="$OC_CONTROL_PLANE_PROBE_HEADERS_FILE"
   export OC_PATCH_CUSTOMER_CONFIG='<absolute-config.yml>'
   ```

2. Generate the three kits:

   ```bash
   bash factory/scripts/prepare.sh '<region>' "$OC_PATCH_CUSTOMER_CONFIG"
   ```

   Read `environment.json`, every `PLAN.json`, `REVIEW.json`, and
   `CLAUDE-REVIEW.txt`. Review must be at least 6.5 with zero blockers.

3. Print each interview and show the operator the exact API ID, stage, URL, and
   the statement that proxy resources are invalid. Record `yes` only after the
   operator confirms them.

4. Apply through the first kit's packaged `runtime/scripts/patch-set.sh`, in
   this dependency order:

   ```text
   114-tenant-stats-table
   114-api-lambda
   114-tenants-stats-route
   ```

   The first kit creates the table, writer IAM/Lambda, initial snapshot, and
   EventBridge schedule. The second updates the API Lambda. The third creates
   the explicit REST method by copying the deployed `GET /tenants`
   authorization, authorizer, scopes, API-key requirement, and Lambda alias.

5. Completion requires `SET_COMPLETE`, a real authenticated
   `GET /tenants-stats` returning HTTP 200 with a `business` object, and the
   driver's automatic second run returning `SKIP` with zero writes.

Never invoke `lib/compiled/*` directly. Never edit a reviewed kit. Never run
CDK, setup, or CloudFormation. On any nonzero exit, preserve the output and
follow the documented exit-code branch; do not change generated code to hide
the failure.
