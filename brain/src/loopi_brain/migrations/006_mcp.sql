-- P3a: bearer tokens of the read-only MCP server (spec §10.5, P3 design §3.1). Only a token's SHA-256 is stored; the
-- token itself is shown once, when it is made (Cài đặt or `loopi-brain mcp-token create`). A revoked token keeps
-- its row (the page lists it as "đã thu hồi").
CREATE TABLE mcp_token (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
    token_sha256  TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
