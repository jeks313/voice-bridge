#!/bin/bash
# khelt WSL: install the voice-bridge TTS supervisor as a systemd service (Chris runs this).
set -euo pipefail
mkdir -p ~/voice-bridge/tts-host
cp /mnt/c/Users/chris/drama_supervisor.py /mnt/c/Users/chris/voice-tts.service ~/voice-bridge/tts-host/
sudo cp ~/voice-bridge/tts-host/voice-tts.service /etc/systemd/system/voice-tts.service
sudo systemctl daemon-reload
sudo systemctl enable --now voice-tts
sleep 2
systemctl is-active voice-tts
curl -s localhost:8091/status; echo
