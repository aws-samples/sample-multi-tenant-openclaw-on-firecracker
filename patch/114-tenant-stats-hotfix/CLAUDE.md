# DISABLED: tenant statistics hotfix factory

This patch factory is fail-closed. Do not generate, review, or run its kits.

## Hard Rules

- Do not run `factory/scripts/prepare.sh`, `materialize-patch.py`,
  `compile-kit.sh`, `patch-set.sh`, or `autopatch.sh`.
- Do not run any previously generated kit from this factory.
- Do not bypass the guards or invoke generated `lib/compiled/*` scripts.
- Do not improvise AWS write commands, CDK, setup, or CloudFormation deployment
  as a substitute.

## Why It Is Disabled

The factory creates only a DynamoDB table, an API Lambda update, and an API
Gateway route. That is not the complete tenant statistics feature. It omits:

1. the tenant-stats writer Lambda;
2. the writer's IAM permissions and environment;
3. the EventBridge schedule that invokes the writer;
4. an authenticated HTTP end-to-end test against the deployed route.

The route manifest also hard-codes `authorization_type=NONE` with
`api_key_required=true`. In platform-key mode that can bypass the platform
`CUSTOM` authorizer.

## Re-enable Gate

Keep the factory disabled until one reviewed patch includes all four missing
pieces, preserves the live platform authorizer configuration, and passes a real
authenticated HTTP end-to-end test in the target environment.
