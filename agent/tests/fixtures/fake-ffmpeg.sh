#!/bin/sh
# Test double for ffmpeg as a network camera's recorder. Video: the last argument is the segment pattern
# <dir>/v_%06d.h264; write a numbered segment every 0.2 s until SIGINT/SIGTERM. Sound: the last argument is pipe:1;
# write silence (s16le) until killed. FAKE_FFMPEG_FAIL=1 prints an RTSP sign-in error that quotes the URL and exits 1.
for last; do :; done
if [ "${FAKE_FFMPEG_FAIL:-}" = 1 ]; then
  for a; do case "$a" in rtsp://*|http://*) url="$a";; esac; done
  echo "[rtsp @ 0x55] method DESCRIBE failed: 401 Unauthorized ($url)" >&2
  exit 1
fi
trap 'exit 0' INT TERM
if [ "$last" = "pipe:1" ]; then
  exec cat /dev/zero
fi
dir=$(dirname "$last")
i=0
while :; do
  printf 'segment-%d' "$i" > "$dir/$(printf 'v_%06d.h264' "$i")"
  i=$((i + 1))
  sleep 0.2
done
