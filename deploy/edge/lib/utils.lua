-- deploy/edge/lib/utils.lua
--
-- Small non-ngx-specific helpers. Kept dependency-free so unit tests can
-- require this module without booting the OpenResty runtime.
--
-- Contract: see project interface spec §1 (Redis key schema) — the JSON
-- decoder here is the only place that must agree with host-agent's writer.

local _M = { _VERSION = "0.01" }

-- is_blank: nil / empty / whitespace-only string all treated as absent.
-- host-agent writes real values; anything that shows up here as "" or " "
-- came from ngx.var defaults or a stripped header — same failure mode.
function _M.is_blank(s)
    if s == nil then return true end
    if type(s) ~= "string" then return false end
    if s == "" then return true end
    return s:find("^%s*$") ~= nil
end

-- safe_tonumber: strict integer parse. Returns nil for anything that isn't
-- a base-10 integer literal (blocks scientific notation / hex / leading +).
-- Ports and epoch timestamps in the route value are integers; a permissive
-- tonumber() would happily accept "1e5" or "0x00" and hand us surprises.
function _M.safe_tonumber(s)
    if type(s) ~= "string" and type(s) ~= "number" then return nil end
    if type(s) == "number" then
        if s ~= math.floor(s) then return nil end
        return s
    end
    if not s:find("^%-?%d+$") then return nil end
    return tonumber(s)
end

-- clone_shallow: 1-level copy used to freeze cached descriptors before
-- returning them to the caller. Prevents downstream mutation from bleeding
-- back into the lrucache/shared_dict cell.
function _M.clone_shallow(t)
    if type(t) ~= "table" then return t end
    local out = {}
    for k, v in pairs(t) do out[k] = v end
    return out
end

return _M
