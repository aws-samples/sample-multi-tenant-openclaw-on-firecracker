-- deploy/edge/lib/tenant.lua
--
-- extract_tenant_id — pull the tenant id out of the request. Two sources
-- in priority order:
--   1. Path: /ws/{tenant_id}/... (production entry, ALB forwards raw URI).
--   2. Header: X-Tenant-Id (fallback for /v1/* alt entry, less common).
--
-- Adversarial inputs we defend against (matches 03-TEST-PLAN):
--   - URL-encoded bytes (%2F etc) — we DO NOT decode; ids are opaque tokens
--     and any decode-then-match would open up path-traversal ambiguity.
--   - Overlong ids (DoS / memory) — cap at MAX_LEN.
--   - Slash injection ("/ws/a/b/..") — split on the first "/" only.
--   - base64 garbage / control bytes — allow-list charset.
--   - Empty / missing — 404 not 400 to hide existence (SEC-4 fail-closed).
--
-- Return values:
--   tenant_id, err_status
-- Where err_status is 400 (malformed path) or 404 (empty / bad charset).
-- The caller (route.lua) turns err_status into ngx.exit; this module keeps
-- ngx side-effects out for testability.

local utils = require "edge.lib.utils"

local _M = { _VERSION = "0.01" }

-- Tenant ids on the platform are UUID (36 chars with dashes) or self-service
-- prefixed (e.g. "u-<slug>"). Cap generously; anything longer is not us.
local MAX_LEN = 96
-- Allowed charset: hex + dashes + underscores + lowercase letters + digits.
-- Explicitly no dots, slashes, percent signs, colons, spaces, unicode.
local ALLOWED = "^[%w_%-]+$"

-- extract_from_path: parses "/ws/<tid>/..." with a single anchored match.
-- We do NOT call ngx.re.match here to keep the module PCRE-free and lua-only
-- (busted tests run under plain Lua without ngx.re). The Lua pattern engine
-- is sufficient because the id charset is a strict subset.
local function extract_from_path(uri)
    if utils.is_blank(uri) then return nil end
    -- Match /ws/<id> or /ws/<id>/rest. The "$" branch handles the exact
    -- "/ws/<id>" no-trailing-slash case.
    local tid = uri:match("^/ws/([^/]+)/") or uri:match("^/ws/([^/]+)$")
    return tid
end

local function extract_from_header(header_val)
    if utils.is_blank(header_val) then return nil end
    return header_val
end

-- validate: run charset + length checks. Returns tenant_id or nil.
local function validate(tid)
    if utils.is_blank(tid) then return nil end
    if #tid > MAX_LEN then return nil end
    if not tid:match(ALLOWED) then return nil end
    return tid
end

--[[
    Public entry point. Two args (both plain strings or nil):
      - uri: ngx.var.uri (or the raw request URI)
      - header_val: request header X-Tenant-Id

    Returns:
      tenant_id (string), nil on success
      nil, err_status (400|404) on failure
--]]
function _M.extract_tenant_id(uri, header_val)
    -- Try path first, then header. Path is authoritative on production
    -- entry; header is a compat path for the /v1/* alt entry.
    local raw = extract_from_path(uri) or extract_from_header(header_val)
    if raw == nil then
        -- Nothing to work with: return 400 for truly malformed paths that
        -- don't parse as /ws/*, else 404 (hides existence).
        if uri and uri:find("^/ws/") then
            return nil, 404  -- "/ws/" or "/ws/?" — no id segment
        end
        return nil, 400
    end
    local tid = validate(raw)
    if tid == nil then
        -- Extracted something but it fails charset/length. Treat as 404 to
        -- avoid leaking whether the id shape is close to real.
        return nil, 404
    end
    return tid, nil
end

-- Exposed for tests.
_M._MAX_LEN = MAX_LEN
_M._ALLOWED = ALLOWED

return _M
