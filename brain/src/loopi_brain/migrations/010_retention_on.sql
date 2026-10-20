-- P3c-1 (design 2026-10-19-p3c-multicam-design.md §4.2, D32, which changes D20): retention is on by default, 7 days
-- of raw video and audio. A running brain gets it switched on here; days the owner stored stay as they are. The
-- owner approved D32 on 2026-10-19.
INSERT INTO setting (key, value) VALUES ('retention', '{"video_days": 7, "audio_days": 7, "enabled": true}'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = jsonb_set(setting.value, '{enabled}', 'true'::jsonb), updated_at = now();
