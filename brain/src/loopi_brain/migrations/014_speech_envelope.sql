-- P3c-3b (the owner's I3 ruling, 2026-10-21): each speech region's loudness envelope (dBFS every 100 ms of the raw
-- audio), so grouping covers a region only by the same sound on another microphone, not by time alone. NULL for
-- regions stored before this version (they keep the time-only rule).
ALTER TABLE speech_region ADD COLUMN envelope real[];
