# M0 utterance recorder for the voice bench. Stdlib Python, no dependencies.
FROM docker.io/library/python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4

# Debian security fixes land in the archive days or weeks before the upstream
# python image is rebuilt, so the pinned digest alone leaves fixable CVEs
# (zlib1g, apt) in the image. Pull them in at build time.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=644 recorder/server.py recorder/index.html recorder/listen.html bench/prompts.json /app/
RUN chmod 755 /app
USER 1000:1000
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
