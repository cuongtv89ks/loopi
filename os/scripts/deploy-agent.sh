#!/usr/bin/env bash
# Rebuild the agent with Buildroot and hot-swap it on a dev device (no reflash).
# Usage: os/scripts/deploy-agent.sh <device-ip>
set -euo pipefail
HOST="${1:?usage: deploy-agent.sh <device-ip>}"
OS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$OS_DIR/build.sh" agent
rsync -z "$OS_DIR/out/loopi-agent" "root@$HOST:/data/dev/loopi-agent.new"
ssh "root@$HOST" 'chmod 755 /data/dev/loopi-agent.new \
  && mv /data/dev/loopi-agent.new /data/dev/loopi-agent \
  && systemctl restart loopi-agent && systemctl is-active loopi-agent'
