CREATE TABLE device (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    cert_fingerprint   TEXT NOT NULL UNIQUE,
    hw_info            JSONB NOT NULL DEFAULT '{}'::jsonb,
    revoked            BOOLEAN NOT NULL DEFAULT FALSE,
    last_heartbeat     JSONB,
    last_heartbeat_at  TIMESTAMPTZ,
    dropped_total      BIGINT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE pair_token (
    token       TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    device_id   TEXT REFERENCES device(id)
);

CREATE TABLE item (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL REFERENCES device(id),
    kind         TEXT NOT NULL CHECK (kind IN ('video', 'audio', 'marker')),
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ NOT NULL,
    sha256       TEXT NOT NULL,
    size_bytes   BIGINT NOT NULL,
    raw_path     TEXT NOT NULL,
    view_path    TEXT,
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT NOT NULL CHECK (status IN ('stored', 'ready', 'error')),
    error        TEXT,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT item_time_order CHECK (ended_at >= started_at)
);
CREATE INDEX item_started_at ON item (started_at);

CREATE TABLE device_command (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES device(id),
    type        TEXT NOT NULL CHECK (type IN ('pause', 'resume', 'set_config')),
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    acked_at    TIMESTAMPTZ
);
CREATE INDEX device_command_pending ON device_command (device_id, id) WHERE acked_at IS NULL;
