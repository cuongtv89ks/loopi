-- P4a: OTA updates (design 2026-10-18-p4a-prod-ota-design.md §6-§8, spec D27, D29). The brain records what the owner
-- asked a device to install and what the device last said about it; the device is the source of truth.
ALTER TABLE device_command DROP CONSTRAINT device_command_type_check;
ALTER TABLE device_command ADD CONSTRAINT device_command_type_check
    CHECK (type IN ('pause', 'resume', 'set_config', 'update'));

CREATE TABLE ota_update (
    id            TEXT PRIMARY KEY,                    -- the update command's id
    device_id     TEXT NOT NULL REFERENCES device(id),
    bundle        TEXT NOT NULL,                       -- kept after the bundle file is deleted
    version       TEXT NOT NULL,
    variant       TEXT NOT NULL CHECK (variant IN ('dev', 'prod')),
    state         TEXT NOT NULL DEFAULT 'requested' CHECK (state IN (
                      'requested', 'downloading', 'installing', 'rebooting', 'trying',
                      'succeeded', 'rolled_back', 'failed')),
    error         TEXT,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- the last report (or the request): expiry counts from here
    finished_at   TIMESTAMPTZ                          -- set with a final state; NULL while the update runs
);
-- At most one unfinished update per device: a second click (another tab) loses the race here.
CREATE UNIQUE INDEX ota_update_one_unfinished ON ota_update (device_id) WHERE finished_at IS NULL;
CREATE INDEX ota_update_device_recent ON ota_update (device_id, requested_at DESC);
