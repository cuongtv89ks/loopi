CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Existing P1 rows become 'pending', so recordings made before the upgrade are analysed too.
ALTER TABLE item
    ADD COLUMN analysis_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (analysis_state IN ('pending', 'done', 'skipped')),
    ADD COLUMN analysis_job_id BIGINT,
    ADD COLUMN analysis_error TEXT,
    ADD COLUMN analyzed_at TIMESTAMPTZ;
CREATE INDEX item_analysis_pending ON item (started_at) WHERE analysis_state = 'pending';

CREATE TABLE keyframe (
    id              TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    device_id       TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    frame_index     INT NOT NULL,
    path            TEXT NOT NULL,
    dhash           BIGINT NOT NULL,
    luma            REAL NOT NULL,
    caption         JSONB,
    caption_model   TEXT,
    captioned_at    TIMESTAMPTZ,
    caption_job_id  BIGINT,
    caption_error   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (item_id, frame_index)
);
CREATE INDEX keyframe_device_ts ON keyframe (device_id, ts);
CREATE INDEX keyframe_uncaptioned ON keyframe (ts) WHERE caption IS NULL;

CREATE TABLE transcript_segment (
    id              TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    device_id       TEXT NOT NULL,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    lang            TEXT NOT NULL,
    text            TEXT NOT NULL,
    avg_logprob     REAL,
    no_speech_prob  REAL,
    model_version   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX transcript_segment_start ON transcript_segment (start_ts);
CREATE INDEX transcript_segment_item ON transcript_segment (item_id);

CREATE TABLE moment (
    id                     TEXT PRIMARY KEY,
    device_id              TEXT NOT NULL,
    ts_start               TIMESTAMPTZ NOT NULL,
    ts_end                 TIMESTAMPTZ NOT NULL,
    kind                   TEXT NOT NULL CHECK (kind IN ('visual', 'speech', 'marker')),
    text                   TEXT NOT NULL,
    lang                   TEXT,
    item_id                TEXT REFERENCES item(id) ON DELETE CASCADE,
    keyframe_id            TEXT REFERENCES keyframe(id) ON DELETE CASCADE,
    transcript_segment_id  TEXT REFERENCES transcript_segment(id) ON DELETE CASCADE,
    embedding              vector(1024),
    embed_model            TEXT,
    model_version          TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX moment_ts_start ON moment (ts_start);
CREATE INDEX moment_embedding ON moment USING hnsw (embedding vector_cosine_ops);
CREATE INDEX moment_text_trgm ON moment USING gin (text gin_trgm_ops);
CREATE INDEX moment_unembedded ON moment (created_at) WHERE embedding IS NULL;
-- Foreign keys that cascade: without these, every deleted item, keyframe or segment scans moment.
CREATE INDEX moment_item ON moment (item_id);
CREATE INDEX moment_keyframe ON moment (keyframe_id);
CREATE INDEX moment_transcript_segment ON moment (transcript_segment_id);
