#!/bin/sh
# Test double for `loopi-agent run --config <dir>/loopi.toml`: appends "<pid> <config>" to <dir>/runs.log, says hello
# on stderr, and runs until SIGTERM. FAKE_AGENT_EXIT=1 makes it exit at once.
config="$3"
dir=$(dirname "$config")
echo "$$ $config" >> "$dir/runs.log"
echo "hello from the agent" >&2
[ "${FAKE_AGENT_EXIT:-}" = 1 ] && exit 3
trap 'exit 0' TERM
while :; do sleep 0.1; done
