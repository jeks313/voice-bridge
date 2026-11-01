# Voice bridge M1: phone app, Whisper STT (GTX 1080), dedicated Claude Code session, TTS client.
# Claude Code, its config and the vault are mounted from the host at their real paths (see k3s/voice-bridge.yaml).
FROM docker.io/library/python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487

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
