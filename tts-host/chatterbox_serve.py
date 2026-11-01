# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = ["chatterbox-tts", "setuptools<81"]
# ///
"""Chatterbox Turbo behind the same small API drama-rs serves, for the voice bridge.

  GET  /health                       -> ok
  GET  /v1/voices                    -> ["seed8", ...]
  POST /v1/speak {"text", "voice"}   -> 16-bit mono WAV at the model's rate (24 kHz)

Voices are VOICES_DIR/*.json ({"name", "wav", "text"}); each reference clip is
embedded once at startup, so a request only pays for generation. One GPU
generation at a time. Never plays audio. Chatterbox adds Resemble's inaudible
Perth watermark to everything it renders.
"""

import argparse
import io
import json
import os
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch
from chatterbox.tts_turbo import ChatterboxTurboTTS

VOICES = os.environ.get("VOICES_DIR", os.path.expanduser("~/Data/voice-bridge/voices"))

model = None
conds = {}
lock = threading.Lock()


def load():
    global model
    t0 = time.time()
    model = ChatterboxTurboTTS.from_pretrained(device="cuda")
    for f in sorted(os.listdir(VOICES)):
        if f.endswith(".json"):
            v = json.load(open(os.path.join(VOICES, f)))
            model.prepare_conditionals(os.path.join(VOICES, v["wav"]))
            conds[v["name"]] = model.conds
    model.generate("Ready.")  # warm the kernels
    print(f"chatterbox-turbo ready in {time.time() - t0:.1f}s, voices {sorted(conds)}", flush=True)


def speak(text, voice):
    with lock:
        if voice in conds:
            model.conds = conds[voice]
        t0 = time.time()
        wav = model.generate(text)
        pcm = (wav.squeeze().cpu().numpy().clip(-1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(model.sr)
        w.writeframes(pcm.tobytes())
    print(f"spoke {len(pcm) / model.sr:.2f}s of audio in {time.time() - t0:.2f}s ({len(text)} chars)", flush=True)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if self.path == "/v1/voices":
            return self._send(200, json.dumps(sorted(conds)).encode(), "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/speak":
            return self._send(404, b"not found", "text/plain")
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            req = {"text": raw.decode(errors="replace")}
        text = (req.get("text") or "").strip()
        if not text:
            return self._send(400, b"no text", "text/plain")
        try:
            self._send(200, speak(text, req.get("voice", "")), "audio/wav")
        except Exception as e:
            self._send(500, str(e).encode(), "text/plain")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    load()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
