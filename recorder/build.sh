#!/usr/bin/env bash
# Build the recorder image straight into k3s's containerd (no registry).
# After a rebuild: kubectl rollout restart deployment/voice-bench
set -euo pipefail
cd "$(dirname "$0")/.."
sudo -n "${NERDCTL:-$HOME/.local/bin/nerdctl-k3s}" build \
  --label "org.opencontainers.image.source=https://github.com/jeks313/voice-bridge" \
  -f recorder/Dockerfile -t voice-bench:current .
echo "built voice-bench:current"
