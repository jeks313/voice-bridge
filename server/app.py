"""Voice bridge M1: push-to-talk phone app <-> dedicated Claude Code session.

WebSocket /ws protocol (JSON text frames, audio as binary frames):
  client -> {"type":"hello","token":...}           server warms TTS, replies {"type":"ready",...}
  client -> {"type":"utterance","mime":...} + one binary frame (the recording)
  client -> {"type":"text","text":...}              typed turn, same path minus STT
  client -> {"type":"new_conversation"}
  server -> {"type":"transcript","text"} | {"type":"say","text","seq"} + binary WAV
            {"type":"file","path"} | {"type":"turn_done","timing":{...}} | {"type":"error","text"}
"""

import asyncio
import json
import os
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from brain import Brain
from speech import STT, TTS, GLOSSARY, SentenceSplitter

APP = Path(__file__).parent
STATE = Path(os.environ.get("STATE_DIR", "/state"))
VAULT = Path(os.environ.get("BRAIN_CWD", os.path.expanduser("~/Data/knowledge"))).resolve()
LOG = STATE / "turns.jsonl"


def pairing_token():
    """VOICE_TOKEN, or a key generated once into STATE_DIR (VOICE_TOKEN=off disables pairing for dev)."""
    if os.environ.get("VOICE_TOKEN"):
        return "" if os.environ["VOICE_TOKEN"] == "off" else os.environ["VOICE_TOKEN"]
    f = STATE / "pairing-key"
    if not f.exists():
        import secrets
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_urlsafe(24))
        f.chmod(0o600)
    return f.read_text().strip()


TOKEN = pairing_token()

contract = (APP / "contract.md").read_text().replace("{glossary}", ", ".join(GLOSSARY))
brain = Brain(contract)
stt = STT()
tts = TTS()


async def run_turn(ws, text, timing):
    """Send one user turn to Claude and stream spoken sentences back as they complete."""
    splitter = SentenceSplitter()
    queue: asyncio.Queue = asyncio.Queue()
    files = []

    async def speaker():
        seq, tts_ok = 0, True
        while (sentence := await queue.get()) is not None:
            audio = None
            if tts_ok:
                try:
                    audio = await tts.speak(sentence)
                except Exception as e:  # keep the turn going; the text still shows on screen
                    tts_ok = False
                    await ws.send_json({"type": "error", "text": f"Voice unavailable ({e.__class__.__name__}); showing text only"})
            await ws.send_json({"type": "say", "text": sentence, "seq": seq, "audio": bool(audio)})
            if audio:
                if seq == 0:
                    timing["first_audio"] = time.time()
                await ws.send_bytes(audio)
            seq += 1

    spk = asyncio.create_task(speaker())
    async for kind, val in brain.turn(text):
        if kind == "text":
            timing.setdefault("first_token", time.time())
            for s in splitter.feed(val):
                await queue.put(s)
        elif kind == "file":
            files.append(val)
            await ws.send_json({"type": "file", "path": val})
        elif kind == "done":
            for s in splitter.flush():
                await queue.put(s)
            timing["result"] = val
    await queue.put(None)
    await spk
    timing["done"] = time.time()
    return files


def summarize(timing):
    t0 = timing["end_of_speech"]
    rel = {k: round(v - t0, 2) for k, v in timing.items() if isinstance(v, float) and k != "end_of_speech"}
    return {**rel, "duration_ms": (timing.get("result") or {}).get("duration_ms")}


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)
    authed = not TOKEN
    pending_mime = None
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            m = json.loads(msg.data)
            if m["type"] == "hello":
                authed = authed or m.get("token") == TOKEN
                if not authed:
                    await ws.send_json({"type": "error", "text": "bad token"})
                    break
                state = await tts.warm()
                await ws.send_json({"type": "ready", "tts": state, "session": brain.session_id})
            elif not authed:
                break
            elif m["type"] == "utterance":
                pending_mime = m.get("mime", "audio/mp4")
            elif m["type"] == "text":
                timing = {"end_of_speech": time.time()}
                await turn(ws, m["text"], timing)
            elif m["type"] == "new_conversation":
                sid = await brain.start(new=True)
                await ws.send_json({"type": "ready", "tts": await tts.status(), "session": sid})
            elif m["type"] == "status":
                await ws.send_json({"type": "status", "tts": await tts.status()})
        elif msg.type == WSMsgType.BINARY and authed and pending_mime:
            timing = {"end_of_speech": time.time()}
            pending_mime = None
            try:
                text = await stt.transcribe(msg.data)
            except Exception as e:
                await ws.send_json({"type": "error", "text": f"speech recognition failed: {e}"})
                await ws.send_json({"type": "turn_done", "timing": summarize(timing), "empty": True})
                continue
            timing["transcript"] = time.time()
            await ws.send_json({"type": "transcript", "text": text})
            if text:
                await turn(ws, text, timing)
            else:
                await ws.send_json({"type": "turn_done", "timing": summarize(timing), "empty": True})
    return ws


async def turn(ws, text, timing):
    try:
        files = await run_turn(ws, text, timing)
    except Exception as e:
        await ws.send_json({"type": "error", "text": f"turn failed: {e}"})
        files = []
    summary = summarize(timing)
    await ws.send_json({"type": "turn_done", "timing": summary, "files": files})
    with open(LOG, "a") as f:
        f.write(json.dumps({"at": time.time(), "text": text, "files": files, "timing": summary,
                            "session": brain.session_id}) + "\n")
    print("turn", json.dumps(summary), flush=True)


async def file_handler(request):
    if TOKEN and request.query.get("token") != TOKEN:
        raise web.HTTPForbidden()
    p = Path(request.query.get("path", "")).resolve()
    if not p.is_file() or VAULT not in p.parents:
        raise web.HTTPNotFound()
    return web.Response(text=p.read_text(errors="replace"), content_type="text/plain")


async def index(request):
    return web.FileResponse(APP / "static" / "index.html", headers={"Cache-Control": "no-store"})


async def healthz(request):
    return web.Response(text="ok")


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/api/file", file_handler),
                    web.get("/healthz", healthz), web.static("/static", APP / "static")])
    web.run_app(app, port=int(os.environ.get("PORT", "8080")), access_log=None)


if __name__ == "__main__":
    main()
