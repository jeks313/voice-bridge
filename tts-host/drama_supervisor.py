"""Load-on-demand front for the voice bridge's TTS engine on khelt (WSL).

ENGINE picks what it starts: "chatterbox-turbo" (default since 2026-11-01:
about 3 GB of VRAM, so Windows doesn't page it out beside the desktop) or
"drama" (Drama 3 int4, 21 GB). Both serve the same /health and /v1/speak.


Listens on 127.0.0.1:8091 (published to the tailnet by `tailscale serve` on
Windows). Starts Drama 3 on the first request or POST /warm, proxies /v1/*
to it (streaming responses pass through as they arrive), and stops it after
IDLE_SECS without requests so the 4090 is free for games.

  GET  /status  -> {"state": "stopped|loading|ready", "idle_for": s}
  POST /warm    -> starts loading, returns at once
  POST /unload  -> stops Drama now
  *    /v1/...  -> proxied to drama serve (waits for it to be ready)

Stdlib only. Never plays audio.
"""

import http.client
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
DRAMA = os.environ.get("DRAMA_BIN", f"{HOME}/voice-bench/drama-rs/target/release/drama")
MODEL = os.environ.get("DRAMA_MODEL_DIR", f"{HOME}/voice-bench/models/drama-3-q4")
VOICES = os.environ.get("VOICES_DIR", f"{HOME}/Data/voice-bridge/voices")
UP_PORT = int(os.environ.get("DRAMA_PORT", "8090"))
LISTEN = ("127.0.0.1", int(os.environ.get("PORT", "8091")))
IDLE_SECS = int(os.environ.get("IDLE_SECS", "600"))
LOAD_TIMEOUT = 240
ENGINE = os.environ.get("ENGINE", "chatterbox-turbo")
HERE = os.path.dirname(os.path.abspath(__file__))

lock = threading.Lock()
proc = None
state = "stopped"
last_use = time.time()
ready = threading.Event()


def voice_args():
    args = []
    for f in sorted(os.listdir(VOICES)) if os.path.isdir(VOICES) else []:
        if f.endswith(".json"):
            v = json.load(open(os.path.join(VOICES, f)))
            args += ["--voice", f"{v['name']}={os.path.join(VOICES, v['wav'])}={v['text']}"]
    return args


def start():
    """Start drama if it isn't running; returns immediately."""
    global proc, state
    with lock:
        if proc and proc.poll() is None:
            return
        ready.clear()
        state = "loading"
        if ENGINE == "drama":
            cmd = [DRAMA, "serve", "--model", MODEL, "--experts", "q4", "--addr", f"127.0.0.1:{UP_PORT}",
                   "--max-len", "2048", *voice_args()]
        else:
            cmd = ["uv", "run", "-q", os.path.join(HERE, "chatterbox_serve.py"), "--port", str(UP_PORT)]
        log = open(f"{HOME}/voice-bench/{ENGINE}-serve.log", "ab")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=f"{HOME}/voice-bench")
        threading.Thread(target=wait_ready, args=(proc,), daemon=True).start()


def wait_ready(p):
    global state
    t0 = time.time()
    while time.time() - t0 < LOAD_TIMEOUT and p.poll() is None:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{UP_PORT}/health", timeout=1)
            state = "ready"
            ready.set()
            print(f"drama ready in {time.time() - t0:.1f}s", flush=True)
            return
        except OSError:
            time.sleep(0.5)
    print("drama failed to start", flush=True)
    stop()


def stop():
    global proc, state
    with lock:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc, state = None, "stopped"
        ready.clear()


def reaper():
    while True:
        time.sleep(15)
        if state == "ready" and time.time() - last_use > IDLE_SECS:
            print(f"idle {IDLE_SECS}s, unloading", flush=True)
            stop()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            return self._json(200, {"state": state, "engine": ENGINE, "idle_for": round(time.time() - last_use)})
        self.proxy()

    def do_POST(self):
        global last_use
        if self.path == "/warm":
            last_use = time.time()
            start()
            return self._json(200, {"state": state})
        if self.path == "/unload":
            stop()
            return self._json(200, {"state": state})
        self.proxy()

    def proxy(self):
        global last_use
        if not self.path.startswith("/v1/"):
            return self._json(404, {"error": "not found"})
        last_use = time.time()
        start()
        if not ready.wait(LOAD_TIMEOUT):
            return self._json(503, {"error": "drama not ready"})
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        up = http.client.HTTPConnection("127.0.0.1", UP_PORT, timeout=120)
        up.request(self.command, self.path, body, {"Content-Type": self.headers.get("Content-Type", "application/json")})
        r = up.getresponse()
        self.send_response(r.status)
        self.send_header("Content-Type", r.getheader("Content-Type", "application/octet-stream"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := r.read1(16384):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
            last_use = time.time()
        self.wfile.write(b"0\r\n\r\n")
        up.close()

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=reaper, daemon=True).start()
    print(f"listening on {LISTEN[0]}:{LISTEN[1]}, idle unload after {IDLE_SECS}s", flush=True)
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
