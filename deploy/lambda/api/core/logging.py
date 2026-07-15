"""Structured logging for the openclaw-api Lambda.

Provides a pre-configured Powertools Logger with trace_root injection.
Usage in handler:

    from core.logging import logger, inject_trace_root

    @logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
    def lambda_handler(event, context):
        inject_trace_root()
        ...

Refs: #209 Task 2 (platform-observability spec).
"""

import os

from aws_lambda_powertools import Logger
from aws_lambda_powertools.logging import correlation_paths  # noqa: F401 — re-exported

from core.trace_root import extract_trace_root

# POWERTOOLS_SERVICE_NAME env var is set by CDK (or defaults here).
logger = Logger(service=os.environ.get("POWERTOOLS_SERVICE_NAME", "openclaw-api"))

# Per-invocation keys appended via append_keys. The Logger is a module-level
# singleton reused across warm invocations and append_keys is persistent, so a
# key set for tenant A survives into the next request unless cleared at entry.
# A warm container serving tenant A, then a request with no tenant path id,
# would log the second request under tenant A's id — a no-cross-tenant leak at
# the log layer. reset_invocation_keys() must run first in the handler.
_PER_INVOCATION_KEYS = ("trace_root", "tenant_id", "request_id")


def reset_invocation_keys() -> None:
    """Drop per-invocation keys left over from a prior warm invocation.

    Call FIRST in lambda_handler, before inject_trace_root / inject_tenant_id.
    remove_keys is a no-op for keys that were never set, so it is safe on a
    cold start too.
    """
    logger.remove_keys(_PER_INVOCATION_KEYS)


def inject_trace_root() -> str:
    """Extract trace_root from Lambda X-Ray env and append to logger.

    Returns the 24-hex trace_root (or empty string if unavailable).
    Call this early in the handler after inject_lambda_context has run.
    """
    raw = os.environ.get("_X_AMZN_TRACE_ID", "")
    trace_root = extract_trace_root(raw)
    if trace_root:
        logger.append_keys(trace_root=trace_root)
    return trace_root


def inject_tenant_id(tenant_id: str) -> None:
    """Append tenant_id to all subsequent log lines in this invocation."""
    if tenant_id:
        logger.append_keys(tenant_id=tenant_id)
