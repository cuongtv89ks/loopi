-- P2b: retention marks an item (raw_deleted_at) and commits that before it deletes the files, so a reprocess never
-- reads "raw is kept" for a file that is going. raw_files_gone_at says the files (raw file and playback copies) really
-- went; a marked item without it, because the worker died in between, is finished by the next nightly run.
ALTER TABLE item ADD COLUMN raw_files_gone_at TIMESTAMPTZ;
CREATE INDEX item_retention_unfinished ON item (raw_deleted_at)
    WHERE raw_deleted_at IS NOT NULL AND raw_files_gone_at IS NULL;
