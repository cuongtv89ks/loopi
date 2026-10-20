-- P3c-2 (design 2026-10-19-p3c-multicam-design.md §4.1, D30, D36): network cameras added from the Thiết bị page. A
-- camera is an ordinary device to the protocol once its agent pairs; this row keeps what the form said (never the
-- password, which lives only in the camera's loopi.toml, mode 600) and links the device the camera's pair token made.
CREATE TABLE network_camera (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('ip', 'phone', 'esp32', 'other')),
    location    TEXT NOT NULL CHECK (btrim(location) <> '' AND length(location) <= 60),
    video_url   TEXT NOT NULL,
    audio_url   TEXT,
    username    TEXT NOT NULL DEFAULT '',
    has_audio   BOOLEAN NOT NULL,
    device_id   TEXT REFERENCES device(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at  TIMESTAMPTZ
);
CREATE INDEX network_camera_device ON network_camera (device_id);
ALTER TABLE pair_token ADD COLUMN camera_id TEXT REFERENCES network_camera(id);
