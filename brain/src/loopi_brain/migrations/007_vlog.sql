-- P3b: one vlog per local day (spec §9.4 `vlog`, §10.3, D15, D25; P3 design §4). Kept for ever (§9.5); "Quên khoảng
-- thời gian" deletes the vlogs of the days it touches (they may show and say what is forgotten), and the planner makes
-- them again from what is left. A rebuild keeps path/duration/generated_at of the vlog being replaced until the new
-- file is stored (state goes back to 'scripted' meanwhile).
CREATE TABLE vlog (
    day            DATE PRIMARY KEY,
    state          TEXT NOT NULL CHECK (state IN ('scripted', 'rendered', 'empty')),
    script         JSONB,                 -- title and highlights (times, keyframes, lines, sound), see vlog/script.py
    model_version  TEXT,                  -- the script's model, or 'rule' for the fallback script
    source         TEXT CHECK (source IN ('video', 'keyframes')),
    path           TEXT,                  -- vlog/YYYY/MM/DD/<day>.mp4 under the media root; .vtt beside it
    duration_s     REAL,
    generated_at   TIMESTAMPTZ,           -- when the current file was stored
    scripted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    stale          BOOLEAN NOT NULL DEFAULT FALSE,
    error          TEXT,
    error_at       TIMESTAMPTZ
);

-- vlog/material.py reads keyframes by time alone (`ts >= … AND ts < …`, twice per vlog). The existing indexes are
-- (device_id, ts), which PG17 cannot use without the leading column, and a partial one on uncaptioned rows. The table
-- grows by thousands of rows a day and is kept for ever.
CREATE INDEX keyframe_ts ON keyframe (ts);
