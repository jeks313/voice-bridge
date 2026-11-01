#!/usr/bin/env bash
# Build the voice-bridge image straight into k3s's containerd (no registry).
# After a rebuild: kubectl rollout restart deployment/voice-bridge
set -euo pipefail
cd "$(dirname "$0")"
sudo -n "${NERDCTL:-$HOME/.local/bin/nerdctl-k3s}" build \
  --label "org.opencontainers.image.source=https://github.com/jeks313/voice-bridge" \
  -t voice-bridge:current .
echo "built voice-bridge:current"
