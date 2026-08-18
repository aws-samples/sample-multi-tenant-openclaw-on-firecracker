-- deploy/edge/fluent-bit/host/add_timestamp.lua
-- Fluent Bit Lua FILTER: stamp @timestamp (ISO8601 UTC, ms) onto every record.
--
-- Why this exists: the Console log query (deploy/console-bff/logs.mjs) treats
-- @timestamp as a hard dependency — it range-filters on it (logs.mjs:63) and
-- sorts by it (logs.mjs:70). Fluent Bit carries the event time out-of-band
-- (not inside the record) and none of the kinesis_firehose outputs set
-- time_key, so the claw-logs-* indices had no such field at all and every
-- Console query answered:
--
--     aos 400: query_shard_exception: No mapping found for [@timestamp]
--               in order to sort on, index: claw-logs-vm-<date>
--
-- surfacing to the operator as `GET /capi/logs → 502 opensearch unavailable`.
-- That is worse than an empty index: Firehose delivery is 1.0 and every
-- pipeline metric is green, so the first guess is "logs were never collected"
-- and the search goes to Fluent Bit / Firehose, where nothing is wrong.
-- This filter copies the event time into the record so the index gets a real
-- date field to sort and filter on.
--
-- Why a FILTER rather than `time_key` on the outputs: time_key would only give
-- second precision, its timezone is decided by the plugin, and it has to be
-- repeated on (and remembered for) every output. A filter runs once per
-- pipeline, is output-agnostic, and keeps ms precision in explicit UTC.
--
-- This file is deliberately duplicated in host/ and edge/: install-fluent-bit.sh
-- pulls exactly one role prefix from S3 (`.../fluent-bit/<role>/ --recursive`)
-- and the edge baked fallback globs only its own role dir, so a shared copy one
-- level up would reach neither. Same reason parsers.conf is duplicated. Keep the
-- two byte-identical — test/add_timestamp_spec.lua asserts it.

-- ms-precision ISO8601 in UTC. `!` selects UTC; os.date has no sub-second
-- field, so the milliseconds are formatted separately and appended.
local function iso8601_utc_ms(sec, ms)
    return os.date("!%Y-%m-%dT%H:%M:%S", sec) .. string.format(".%03dZ", ms)
end

-- Fluent Bit hands the event time in either shape: a table {sec=, nsec=} when
-- the FILTER sets `time_as_table On`, or a plain Lua number of epoch seconds
-- carrying the sub-second part as a fraction. Accept both so the filter keeps
-- working if that flag is ever dropped from fluent-bit.conf.
--
-- The conf sets time_as_table On for a measured reason: at epoch magnitude a
-- double has only ~6 fractional digits left, so the number path can truncate
-- the last millisecond (1786603686.001 lands at frac 0.000999928 → 0 ms). The
-- table path carries integer nsec and is exact. See test/add_timestamp_spec.lua.
local function split_event_time(timestamp)
    if type(timestamp) == "table" then
        return timestamp.sec or 0, math.floor((timestamp.nsec or 0) / 1000000)
    end
    local ts = timestamp or 0
    local sec = math.floor(ts)
    -- ts - sec is strictly < 1, so ms can never reach 1000 and roll the second.
    return sec, math.floor((ts - sec) * 1000)
end

function add_timestamp(tag, timestamp, record)
    -- Idempotent on purpose: a pipeline whose parser already mapped the
    -- source's own event time into @timestamp keeps it. Only fill the gap.
    if record["@timestamp"] == nil then
        local sec, ms = split_event_time(timestamp)
        record["@timestamp"] = iso8601_utc_ms(sec, ms)
    end
    -- rc=1 = record modified; the event time itself is passed through unchanged.
    return 1, timestamp, record
end
