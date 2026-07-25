# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Domain modules for the api Lambda (T3-4).

The api handler is being split from one ~2800-line module into a thin facade
(handler.py: clients/env, the routes dict, lambda_handler, RBAC) plus stateless
domain modules here. Extraction is incremental and behavior-preserving:

- Pure helpers with no shared state live here directly (common.py).
- Stateful domain logic (tenants, hosts, ...) takes the facade module as an
  explicit context (`_CTX = sys.modules["<facade>"]`) so it late-binds to the
  facade's boto3 clients / DDB tables — which the test suite monkeypatches.
- The facade re-exports every moved symbol, so `api.<name>` keeps resolving for
  the ~30 tests that reference handler attributes and for intra-handler callers.

At runtime this package sits at the Lambda task root (the api bundle copies the
whole dir), so `from domains.common import ...` resolves; tests put
deploy/lambda/api on sys.path (see conftest).
"""
