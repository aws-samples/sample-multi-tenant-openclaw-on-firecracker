-- deploy/edge/test/tenant_spec.lua
--
-- extract_tenant_id — adversarial coverage matrix per 03-TEST-PLAN.

local helper = require "spec_helper"
local tenant = require "edge.lib.tenant"

describe("tenant.extract_tenant_id", function()
    before_each(function() helper.reset_ngx() end)

    -- --- Positive cases -----------------------------------------------------
    it("extracts UUID from /ws/{id}/...", function()
        local tid, err = tenant.extract_tenant_id(
            "/ws/550e8400-e29b-41d4-a716-446655440000/v1/chat/completions", nil)
        assert.is_nil(err)
        assert.are.equal("550e8400-e29b-41d4-a716-446655440000", tid)
    end)

    it("extracts self-service prefix id (u-slug)", function()
        local tid, err = tenant.extract_tenant_id("/ws/u-abel-demo/v1/responses", nil)
        assert.is_nil(err)
        assert.are.equal("u-abel-demo", tid)
    end)

    it("extracts id with no trailing slash", function()
        local tid, err = tenant.extract_tenant_id("/ws/abc123", nil)
        assert.is_nil(err)
        assert.are.equal("abc123", tid)
    end)

    it("falls back to X-Tenant-Id header when path is not /ws/*", function()
        local tid, err = tenant.extract_tenant_id("/v1/chat", "abc123")
        assert.is_nil(err)
        assert.are.equal("abc123", tid)
    end)

    it("prefers path over header when both present", function()
        local tid, err = tenant.extract_tenant_id("/ws/from-path/v1", "from-header")
        assert.is_nil(err)
        assert.are.equal("from-path", tid)
    end)

    -- --- Negative / adversarial --------------------------------------------
    it("returns 400 for URI outside /ws/* with no header", function()
        local tid, err = tenant.extract_tenant_id("/random/path", nil)
        assert.is_nil(tid)
        assert.are.equal(400, err)
    end)

    it("returns 404 for /ws/ with empty id segment", function()
        local tid, err = tenant.extract_tenant_id("/ws/", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("returns 404 for /ws/// with empty segments", function()
        local tid, err = tenant.extract_tenant_id("/ws///", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("returns 404 for nil URI and blank header", function()
        assert.are.equal(400, ({tenant.extract_tenant_id(nil, nil)})[2])
        assert.are.equal(400, ({tenant.extract_tenant_id("", "")})[2])
    end)

    it("rejects URL-encoded path traversal in id", function()
        -- %2E%2E = ".." — never decode; treat as unknown charset
        local tid, err = tenant.extract_tenant_id("/ws/%2E%2E/v1", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("rejects overlong id", function()
        local giant = string.rep("a", tenant._MAX_LEN + 1)
        local tid, err = tenant.extract_tenant_id("/ws/" .. giant .. "/v1", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("rejects dot in id (breaks charset)", function()
        local tid, err = tenant.extract_tenant_id("/ws/foo.bar/v1", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("rejects colon in id", function()
        local tid, err = tenant.extract_tenant_id("/ws/foo:bar/v1", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("rejects control bytes / null byte", function()
        local evil = "abc\0def"
        local tid, err = tenant.extract_tenant_id("/ws/" .. evil .. "/v1", nil)
        assert.is_nil(tid)
        assert.are.equal(404, err)
    end)

    it("accepts max-length id at boundary", function()
        local ok = string.rep("a", tenant._MAX_LEN)
        local tid, err = tenant.extract_tenant_id("/ws/" .. ok .. "/v1", nil)
        assert.is_nil(err)
        assert.are.equal(ok, tid)
    end)

    it("rejects blank-only header fallback", function()
        local tid, err = tenant.extract_tenant_id("/v1/chat", "   ")
        assert.is_nil(tid)
        assert.are.equal(400, err)
    end)
end)
