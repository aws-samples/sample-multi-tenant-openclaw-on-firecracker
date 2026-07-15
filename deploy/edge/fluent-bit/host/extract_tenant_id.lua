-- deploy/edge/fluent-bit/host/extract_tenant_id.lua
-- Fluent Bit Lua FILTER: pull tenant_id from the fc.log source path.
--
-- The tail input sets record["log_path"] = "/data/firecracker-vms/<tid>/fc.log"
-- (Path_Key log_path). We extract <tid> so per-tenant VM logs land in the
-- vm index searchable by tenant_id.
--
-- Invariant (no-cross-tenant at the log layer): a path that does not match
-- the exact expected shape must collapse to tenant_id = "-", never guess a
-- partial or wrong id that could stitch one tenant's VMM log onto another's.

-- A tenant_id is the microVM dir name. Accept only the safe charset
-- (alphanumeric, dash, underscore) so an unexpected path can't inject.
local function tenant_from_path(path)
    if not path or path == "" then return "-" end
    local tid = path:match("^/data/firecracker%-vms/([%w%-_]+)/fc%.log$")
    if tid and tid ~= "" then
        return tid
    end
    return "-"
end

function extract_tenant_id(tag, timestamp, record)
    record["tenant_id"] = tenant_from_path(record["log_path"])
    return 1, timestamp, record
end
