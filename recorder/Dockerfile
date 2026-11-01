# M0 utterance recorder for the voice bench. Stdlib Python, no dependencies.
FROM docker.io/library/python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487
COPY --chmod=644 recorder/server.py recorder/index.html recorder/listen.html bench/prompts.json /app/
RUN chmod 755 /app
USER 1000:1000
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
