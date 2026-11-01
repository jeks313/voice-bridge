"""M0 voice bench: utterance recorder (/) and TTS listening page (/listen).

Stdlib only. Clips land in DATA_DIR/utterances/<prompt id>.<ext>, with
DATA_DIR/utterances/index.jsonl recording what was asked and when. A second
take of a prompt overwrites the first.
"""

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROMPTS = json.loads((APP_DIR / "prompts.json").read_text())["prompts"]
MAX_CLIP = 20 * 1024 * 1024
EXT = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg", "audio/wav": "wav"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        # Byte ranges: iOS Safari refuses to play <audio> without them.
        data = path.read_bytes()
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if not rng:
            self.send_response(200)
            start, end = 0, len(data) - 1
        else:
            a, b = rng.groups()
            start = int(a) if a else max(0, len(data) - int(b))
            end = min(int(b), len(data) - 1) if a and b else len(data) - 1
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(data[start:end + 1])

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if self.path == "/api/prompts":
            done = {p.stem for p in (DATA_DIR / "utterances").glob("*.*") if p.suffix != ".jsonl"}
            return self._send(200, [dict(p, done=p["id"] in done) for p in PROMPTS])
        if self.path == "/api/tts":
            root = DATA_DIR / "tts"
            return self._send(200, sorted(str(f.relative_to(root)) for f in root.rglob("*.wav")) if root.exists() else [])
        m = re.fullmatch(r"/tts/([\w./-]+\.wav)", self.path)
        if m and ".." not in m.group(1) and (DATA_DIR / "tts" / m.group(1)).is_file():
            return self._send_file(DATA_DIR / "tts" / m.group(1), "audio/wav")
        if self.path == "/listen":
            return self._send(200, (APP_DIR / "listen.html").read_bytes(), "text/html; charset=utf-8")
        if self.path in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/pick":
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 4096:
                return self._send(413, {"error": "size"})
            pick = json.loads(self.rfile.read(n))
            with open(DATA_DIR / "picks.jsonl", "a") as f:
                f.write(json.dumps({"file": str(pick.get("file", ""))[:200], "score": int(pick.get("score", 0)),
                                    "note": str(pick.get("note", ""))[:500], "at": time.time()}) + "\n")
            return self._send(200, {"ok": True})
        m = re.fullmatch(r"/api/clip/([a-z0-9]{2,8})", self.path)
        if not m or m.group(1) not in {p["id"] for p in PROMPTS}:
            return self._send(404, {"error": "unknown prompt"})
        n = int(self.headers.get("Content-Length", "0"))
        if not 0 < n <= MAX_CLIP:
            return self._send(413, {"error": "clip size"})
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
        ext = EXT.get(ctype, "bin")
        out = DATA_DIR / "utterances"
        out.mkdir(parents=True, exist_ok=True)
        pid = m.group(1)
        for old in out.glob(f"{pid}.*"):
            old.unlink()
        (out / f"{pid}.{ext}").write_bytes(self.rfile.read(n))
        with open(out / "index.jsonl", "a") as f:
            f.write(json.dumps({"id": pid, "file": f"{pid}.{ext}", "bytes": n, "type": ctype,
                                "ua": self.headers.get("User-Agent", ""), "at": time.time()}) + "\n")
        self._send(200, {"saved": f"{pid}.{ext}"})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
