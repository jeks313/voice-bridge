# Voice bridge M1: phone app, Whisper STT (GTX 1080), dedicated Claude Code session, TTS client.
# Claude Code, its config and the vault are mounted from the host at their real paths (see k3s/voice-bridge.yaml).
FROM docker.io/library/python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4

# Debian security fixes land in the archive days or weeks before the upstream
# python image is rebuilt, so the pinned digest alone leaves fixable CVEs
# (zlib1g, apt) in the image. Pull them in at build time.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN mkdir -p /app/static
COPY --chmod=644 app.py brain.py cues.py herdr_brain.py permission.py speech.py contract.md glossary.txt /app/
COPY --chmod=644 static/ /app/static/

USER 1000:1000
ENV PYTHONUNBUFFERED=1 HOME=/home/jeks PATH=/home/jeks/.local/bin:/usr/local/bin:/usr/bin:/bin \
    GLOSSARY=/app/glossary.txt STATE_DIR=/state PORT=8080
EXPOSE 8080
WORKDIR /app
ENTRYPOINT ["python", "/app/app.py"]
