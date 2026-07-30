"""Extract trace_root from X-Amzn-Trace-Id header variants.

Contract: any header variant → 24-hex Root or empty string.
Three-language reference: Python (Lambda), JS (JDWS/BFF), Lua (edge FILTER).
This file is the Python canonical implementation.
"""

import re

# Precise: 1-<8hex>-<24hex>. Negative lookahead (?![0-9a-fA-F]) rejects an
# oversized id segment (e.g. 28 hex) instead of truncating it into a
# valid-looking 24-hex root — that truncation could collide with a legit
# tenant's trace and stitch two log streams together.
_ROOT_RE = re.compile(r"Root=(1-[0-9a-fA-F]{8}-[0-9a-fA-F]{24})(?![0-9a-fA-F])")
_HEX24_RE = re.compile(r"^[0-9a-f]{24}$")


def extract_trace_root(header_value: str) -> str:
    """Return 24-hex trace root from X-Amzn-Trace-Id or empty string.

    Accepts: 'Root=1-xxx-yyy;Self=...' or bare '1-xxx-yyy' or custom fields.
    Returns the 24-hex portion (segment after second dash) or ''.
    """
    if not header_value:
        return ""

    m = _ROOT_RE.search(header_value)
    if not m:
        parts = header_value.split(";")
        for part in parts:
            part = part.strip()
            if part.startswith("Root="):
                raw = part[5:]
                return _normalize_root(raw)
            if _looks_like_trace_id(part):
                return _normalize_root(part)
        return ""

    return _normalize_root(m.group(1))


def _looks_like_trace_id(val: str) -> bool:
    """Check if value looks like '1-xxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx'."""
    return bool(val) and val.startswith("1-") and len(val) == 35


def _normalize_root(raw: str) -> str:
    """Extract 24-hex from '1-xxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx' format."""
    if not raw:
        return ""
    segments = raw.split("-")
    if len(segments) == 3 and len(segments[2]) == 24:
        hex_part = segments[2].lower()
        if _HEX24_RE.match(hex_part):
            return hex_part
    return ""
