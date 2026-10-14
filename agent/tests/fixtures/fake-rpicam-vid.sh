#!/bin/sh
# Test double for rpicam-vid: writes a numbered segment file every 0.2 s into the -o directory
# until it receives SIGINT/SIGTERM.
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    out="$2"
    shift
  fi
  shift
done
[ -n "$out" ] || { echo "fake-rpicam-vid: missing -o" >&2; exit 2; }
dir=$(dirname "$out")
trap 'exit 0' INT TERM
i=0
while :; do
  printf 'segment-%d' "$i" > "$dir/$(printf 'v_%06d.h264' "$i")"
  i=$((i + 1))
  sleep 0.2
done
