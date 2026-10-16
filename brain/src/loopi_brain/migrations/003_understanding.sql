-- P2b: chapters, day recaps, commitments, editable settings, the "forget a time range" audit,
-- errors of the per-day jobs, and the retention mark on items (spec §9.4, §9.5, §10, D20).

-- Retention deleted the raw file (and its playback copy); the row, keyframes and transcripts stay.
ALTER TABLE item ADD COLUMN raw_deleted_at TIMESTAMPTZ;
CREATE INDEX item_retention ON item (ended_at) WHERE raw_deleted_at IS NULL AND kind IN ('video', 'audio');
-- The hourly chapter planner looks for days whose moments were written recently.
CREATE INDEX moment_created_at ON moment (created_at);

-- One chapter of a local day (all devices). Members are the day's moments whose ts_start lies in
-- [ts_start, last_moment_at]; `signature` hashes their ids, so a summary is rewritten only when they change.
-- Closed chapters are settled and never recomputed until the day is reopened (forget, reprocess).
CREATE TABLE chapter (
    id                 TEXT PRIMARY KEY,
    day                DATE NOT NULL,
    ts_start           TIMESTAMPTZ NOT NULL,
    ts_end             TIMESTAMPTZ NOT NULL,
    last_moment_at     TIMESTAMPTZ NOT NULL,
    moment_count       INT NOT NULL CHECK (moment_count > 0),
    signature          TEXT NOT NULL,
    closed             BOOLEAN NOT NULL DEFAULT FALSE,
    title              TEXT,
    summary            TEXT,
    summary_signature  TEXT,
    model_version      TEXT,
    summary_error      TEXT,
    summary_error_at   TIMESTAMPTZ,            -- the planner leaves a failing chapter alone for a while
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chapter_time_order CHECK (ts_end >= ts_start AND last_moment_at >= ts_start)
);
CREATE INDEX chapter_day ON chapter (day, ts_start);

-- A day with no data never gets a row. `stale`: reprocessing changed the day; the planner writes it again.
CREATE TABLE day_recap (
    day            DATE PRIMARY KEY,
    markdown       TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    stale          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE commitment (
    id                TEXT PRIMARY KEY,
    day               DATE NOT NULL,
    text              TEXT NOT NULL,
    source_moment_id  TEXT REFERENCES moment(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed')),
    model_version     TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX commitment_day ON commitment (day);
CREATE INDEX commitment_source ON commitment (source_moment_id);

-- Settings edited on the Cài đặt page (e.g. retention days); absent keys use the built-in defaults.
CREATE TABLE setting (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit of "Quên khoảng thời gian": what was asked and how much went (no content). `pending_files` lists the media
-- paths still to delete after the commit; the nightly retention run retries them until the list is empty.
CREATE TABLE forget_log (
    id             TEXT PRIMARY KEY,
    range_start    TIMESTAMPTZ NOT NULL,
    range_end      TIMESTAMPTZ NOT NULL,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    counts         JSONB NOT NULL,
    pending_files  JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT forget_range_order CHECK (range_end > range_start)
);

-- Last error of a per-day job (chapters, recap), shown next to its dead letter; cleared on success.
CREATE TABLE day_job_error (
    task   TEXT NOT NULL,
    day    DATE NOT NULL,
    error  TEXT NOT NULL,
    at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task, day)
);
