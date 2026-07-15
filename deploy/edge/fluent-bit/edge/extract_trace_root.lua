-- deploy/edge/fluent-bit/extract_trace_root.lua
-- Fluent Bit Lua FILTER: extract 24-hex trace_root from the "trace" field.
--
-- Contract (Property 1): identical extraction logic to deploy/edge/lib/trace_root.lua.
-- The trace field contains either a raw X-Amzn-Trace-Id header value
-- (e.g. "Root=1-67890abc-abcdef012345abcdef012345;Self=1-...") or a
-- fallback $request_id (32-hex, no Root= prefix).
--
-- Output: adds/overwrites record["trace_root"] with the 24-hex or leaves "-".

local function normalize_root(raw)
    if not raw or raw == "" then return "" end
    local seg3 = raw:match("^%w+-%x+-(%x+)$")
    if seg3 and #seg3 == 24 then
        return seg3:lower()
    end
    return ""
end

local function extract_from_header(header_value)
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

function extract_trace_root(tag, timestamp, record)
    local trace = record["trace_root"]
    -- If nginx already resolved trace_root (via rewrite phase), keep it.
    if trace and trace ~= "" and trace ~= "-" then
        return 0, timestamp, record
    end

    -- Fall back to extracting from the raw "trace" field.
    local raw = record["trace"] or ""
    local root = extract_from_header(raw)
    if root ~= "" then
        record["trace_root"] = root
    else
        record["trace_root"] = "-"
    end
    return 1, timestamp, record
end
