-- Extract trace_root from X-Amzn-Trace-Id header.
-- Contract: any header variant → 24-hex Root or empty string.
-- Lua canonical implementation (OpenResty edge FILTER phase).

local _M = {}

local function normalize_root(raw)
    if not raw or raw == "" then return "" end
    local seg3 = raw:match("^%w+-%x+-(%x+)$")
    if seg3 and #seg3 == 24 then
        return seg3:lower()
    end
    return ""
end

function _M.extract(header_value)
    if not header_value or header_value == "" then return "" end

    local root_val = header_value:match("Root=([%w%-]+)")
    if root_val then
        return normalize_root(root_val)
    end

    for part in header_value:gmatch("[^;]+") do
        local trimmed = part:match("^%s*(.-)%s*$")
        if trimmed:sub(1, 5) == "Root=" then
            return normalize_root(trimmed:sub(6))
        end
        if trimmed:sub(1, 2) == "1-" and #trimmed == 35 then
            return normalize_root(trimmed)
        end
    end
    return ""
end

return _M
