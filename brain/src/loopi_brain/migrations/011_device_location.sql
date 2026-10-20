-- P3c-1 (design 2026-10-19-p3c-multicam-design.md §4.1, D34): every device has an editable location name
-- ("Phòng khách"), used on the timeline, in search, chat, MCP and in the text the chapter and recap models read.
-- It starts as the device's name.
ALTER TABLE device ADD COLUMN location TEXT;
UPDATE device SET location = name;
ALTER TABLE device ALTER COLUMN location SET NOT NULL;
