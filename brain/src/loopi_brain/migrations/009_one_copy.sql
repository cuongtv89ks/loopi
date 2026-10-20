-- P3c-1 (design 2026-10-19-p3c-multicam-design.md §4.2, D32): one copy per video segment. Once a segment has its
-- MP4 (remuxed with -c copy) and its analysis is done, its raw H.264 is deleted and this says when; the MP4 is the
-- copy kept until retention. Not raw_deleted_at, which means the whole recording expired and nothing plays any more.
ALTER TABLE item ADD COLUMN h264_deleted_at TIMESTAMPTZ;
CREATE INDEX item_one_copy_due ON item (started_at)
    WHERE kind = 'video' AND status = 'ready' AND h264_deleted_at IS NULL AND raw_deleted_at IS NULL;
