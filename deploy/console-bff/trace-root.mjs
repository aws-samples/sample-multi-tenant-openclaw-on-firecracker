/**
 * Extract trace_root from X-Amzn-Trace-Id header variants.
 * Contract: any header variant → 24-hex Root or empty string.
 * JS canonical implementation (JDWS + BFF).
 */

// Precise: 1-<8hex>-<24hex>. Negative lookahead rejects an oversized id
// segment instead of truncating it into a valid-looking 24-hex root (that
// truncation could collide with a legit tenant's trace across log streams).
const ROOT_RE = /Root=(1-[0-9a-fA-F]{8}-[0-9a-fA-F]{24})(?![0-9a-fA-F])/;
const HEX24_RE = /^[0-9a-f]{24}$/;

export function extractTraceRoot(headerValue) {
  if (!headerValue) return "";

  const m = ROOT_RE.exec(headerValue);
  if (m) return normalizeRoot(m[1]);

  for (const part of headerValue.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith("Root=")) return normalizeRoot(trimmed.slice(5));
    if (looksLikeTraceId(trimmed)) return normalizeRoot(trimmed);
  }
  return "";
}

function looksLikeTraceId(val) {
  return val && val.startsWith("1-") && val.length === 35;
}

function normalizeRoot(raw) {
  if (!raw) return "";
  const segments = raw.split("-");
  if (segments.length === 3 && segments[2].length === 24) {
    const hex = segments[2].toLowerCase();
    if (HEX24_RE.test(hex)) return hex;
  }
  return "";
}
