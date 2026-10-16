-- P2c: the owner's golden sets and the evaluation runs (spec §13, D21), and Whisper's per-region language
-- probability. Golden-set media are copies under LOOPI_EVAL_DIR (paths here are relative to it), never in git;
-- retention never touches them. "Quên khoảng thời gian" deletes the samples of its range (and the cascades below
-- are the backstop): a sample never outlives the recording it was taken from.

-- The probability Whisper gave the language it chose for the segment's speech region (NULL before P2c).
ALTER TABLE transcript_segment ADD COLUMN lang_prob REAL;

-- A recording the owner listened to and wrote down by hand (the reference transcript), or marked as having no
-- speech at all (silence, TV, noise: the hallucination check). `path`: a byte copy of the raw recording.
-- `had_text`: the live transcript had text when it was labelled (a no-speech clip with text tests the rules);
-- `prefilled`: the reference was edited from Whisper's text (false: typed after listening, without seeing it).
CREATE TABLE eval_audio (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL UNIQUE REFERENCES item(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    lang        TEXT CHECK (lang IN ('vi', 'ko', 'en')),
    no_speech   BOOLEAN NOT NULL DEFAULT FALSE,
    reference   TEXT NOT NULL DEFAULT '',
    had_text    BOOLEAN NOT NULL DEFAULT FALSE,
    prefilled   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT eval_audio_label CHECK (
        CASE WHEN no_speech THEN lang IS NULL AND reference = ''
             ELSE lang IS NOT NULL AND btrim(reference) <> '' END
    )
);

-- A keyframe the owner scores; `path`: a copy of its JPEG.
CREATE TABLE eval_caption (
    id           TEXT PRIMARY KEY,
    keyframe_id  TEXT NOT NULL UNIQUE REFERENCES keyframe(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The owner's score of one caption of a sample, per caption model: a new model's caption is scored again.
CREATE TABLE eval_caption_score (
    sample_id         TEXT NOT NULL REFERENCES eval_caption(id) ON DELETE CASCADE,
    model_version     TEXT NOT NULL,
    caption           JSONB NOT NULL,              -- the caption that was scored
    score             SMALLINT NOT NULL CHECK (score BETWEEN 1 AND 5),
    wrong_people      BOOLEAN NOT NULL DEFAULT FALSE,   -- "sai số người"
    invented_objects  BOOLEAN NOT NULL DEFAULT FALSE,   -- "bịa đồ vật"
    wrong_activity    BOOLEAN NOT NULL DEFAULT FALSE,   -- "sai hoạt động"
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sample_id, model_version)
);

-- A question the owner asks of the memories, with the local days it is about (optional, as the search page's
-- filter: "hôm qua" in a question means the day it was written about, not the day it is evaluated).
CREATE TABLE eval_question (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL CHECK (btrim(text) <> ''),
    first_day   DATE,
    last_day    DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT eval_question_days CHECK (first_day IS NULL OR last_day IS NULL OR first_day <= last_day)
);

-- A correct moment of a question. Moment ids change when a day is reprocessed (new captions, new transcripts), so
-- the answer also keeps what the moment came from: its keyframe (pictures), its recording and time (speech), its
-- marker item; the runner matches on those.
CREATE TABLE eval_answer (
    question_id  TEXT NOT NULL REFERENCES eval_question(id) ON DELETE CASCADE,
    moment_id    TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('visual', 'speech', 'marker')),
    item_id      TEXT REFERENCES item(id) ON DELETE CASCADE,
    keyframe_id  TEXT REFERENCES keyframe(id) ON DELETE CASCADE,
    ts_start     TIMESTAMPTZ NOT NULL,
    ts_end       TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (question_id, moment_id)
);
CREATE INDEX eval_answer_item ON eval_answer (item_id);
CREATE INDEX eval_answer_keyframe ON eval_answer (keyframe_id);

-- One evaluation run: the configuration it ran with (ASR settings and rules, model versions, search thresholds)
-- and its numbers (`results`: numbers only, never text).
CREATE TABLE eval_run (
    id            TEXT PRIMARY KEY,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'running', 'done', 'failed')),
    config        JSONB NOT NULL,
    results       JSONB,
    error         TEXT
);

-- What the current ASR path heard in one golden clip during a run: every Whisper segment with its region, language
-- and probability, before any filter (the rule sets are applied afterwards), or the error.
CREATE TABLE eval_run_audio (
    run_id     TEXT NOT NULL REFERENCES eval_run(id) ON DELETE CASCADE,
    sample_id  TEXT NOT NULL REFERENCES eval_audio(id) ON DELETE CASCADE,
    segments   JSONB,
    error      TEXT,
    PRIMARY KEY (run_id, sample_id)
);
CREATE INDEX eval_run_audio_sample ON eval_run_audio (sample_id);
