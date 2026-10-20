-- P3c-3 (D33, P3c design §4.3): the speech found in each audio item, measured, then grouped across microphones
-- before Whisper runs. `state`: waiting (not decided yet), chosen (Whisper transcribes it), covered (another
-- microphone's chosen region holds the same speech: "đã chép ở mic khác"; `covered_by` points to it). The chosen
-- region records its speech moment in `moment_id` once transcribed.
ALTER TABLE item ADD COLUMN speech_detected_at TIMESTAMPTZ;

CREATE TABLE speech_region (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES item (id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    start_ts TIMESTAMPTZ NOT NULL,
    end_ts TIMESTAMPTZ NOT NULL,
    offset_s DOUBLE PRECISION NOT NULL CHECK (offset_s >= 0),
    length_s DOUBLE PRECISION NOT NULL CHECK (length_s > 0),
    snr_db DOUBLE PRECISION NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'chosen', 'covered')),
    covered_by TEXT REFERENCES speech_region (id) ON DELETE SET NULL,
    moment_id TEXT REFERENCES moment (id) ON DELETE SET NULL,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_ts > start_ts)
);
CREATE INDEX speech_region_item ON speech_region (item_id);
CREATE INDEX speech_region_start ON speech_region (start_ts);
CREATE INDEX speech_region_waiting ON speech_region (start_ts) WHERE state = 'waiting';
CREATE INDEX speech_region_covered_by ON speech_region (covered_by);
CREATE INDEX speech_region_moment ON speech_region (moment_id);
