"""Voice bridge M1: push-to-talk phone app <-> dedicated Claude Code session.

WebSocket /ws protocol (JSON text frames, audio as binary frames):
  client -> {"type":"hello","token":...}           server warms TTS, replies {"type":"ready",...}
  client -> {"type":"utterance","mime":...} + one binary frame (the recording)
  client -> {"type":"text","text":...}              typed turn, same path minus STT
  client -> {"type":"new_conversation"}             (pod brain: fresh session; herdr brain: new topic tab)
  client -> {"type":"topics"} | {"type":"switch_topic","id"} | {"type":"new_topic","name"} | {"type":"rename_topic","id","name"}
  server -> {"type":"topics","items":[{"id","name","status","last","active"}]}
  server -> {"type":"transcript","text"} | {"type":"say","text","seq"} + binary WAV
            {"type":"file","path"} | {"type":"turn_done","timing":{...}} | {"type":"error","text"}
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

from brain import Brain
from herdr_brain import HerdrBrain
import cues
import permission
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
# "herdr" drives a real herdr pane on spiff (M4); "pod" is M1's claude -p session inside this pod
brain = HerdrBrain(contract) if os.environ.get("BRAIN_BACKEND", "pod") == "herdr" else Brain(contract)
stt = STT()
tts = TTS()


# reply sentences that were due while the phone was away (screen locked, connection dropped);
# offered on the next connection as "Play what you missed"
MISSED: list = []


class Conn:
    """One phone connection: serialised sends, at most one turn running, at most one permission pending."""

    def __init__(self, ws):
        self.ws = ws
        self.lock = asyncio.Lock()
        self.turn_task = None
        self.pending = None  # {"id", "info", "fut"} while a permission prompt waits for Chris
        self.lost = False  # the phone went away mid-turn; the turn finishes and its reply is kept in MISSED

    async def send(self, obj):
        if self.lost or self.ws.closed:
            self.lost = True
            return
        try:
            async with self.lock:
                await self.ws.send_json(obj)
        except (ConnectionError, RuntimeError):
            self.lost = True

    async def say(self, text, seq, audio, cue=False, replay=False):
        if not (self.lost or self.ws.closed):
            try:
                async with self.lock:  # the "say" frame and its WAV must stay adjacent
                    await self.ws.send_json({"type": "say", "text": text, "seq": seq, "audio": bool(audio),
                                             "cue": cue, "replay": replay})
                    if audio:
                        await self.ws.send_bytes(audio)
                return
            except (ConnectionError, RuntimeError):
                pass
        self.lost = True
        if not cue and seq >= 0:
            MISSED.append(text)

    async def speak_now(self, text):
        """Speak a line outside a turn (e.g. "say yes or no")."""
        try:
            audio = await tts.speak(text)
        except Exception:
            audio = None
        await self.say(text, -1, audio)

    @property
    def busy(self):
        return self.turn_task is not None and not self.turn_task.done()


async def run_turn(conn, text, timing):
    """Send one user turn to Claude and stream spoken sentences back as they complete."""
    splitter = SentenceSplitter()
    queue: asyncio.Queue = asyncio.Queue()
    files = []

    async def speaker():
        seq, tts_ok = 0, True
        while (sentence := await queue.get()) is not None:
            is_cue = isinstance(sentence, tuple)  # ("cue", text): a status line, not part of the reply
            if is_cue:
                sentence = sentence[1]
            audio = None
            if tts_ok and not (conn.lost or conn.ws.closed):  # nobody to play it to: keep the text only
                try:
                    audio = await tts.speak(sentence)
                except Exception as e:  # keep the turn going; the text still shows on screen
                    tts_ok = False
                    await conn.send({"type": "error", "text": f"Voice unavailable ({e.__class__.__name__}); showing text only"})
            if is_cue:
                await conn.say(sentence, -1, audio, cue=True)
                continue
            if audio and seq == 0:
                timing["first_audio"] = time.time()
            await conn.say(sentence, seq, audio)
            seq += 1

    async def ask(p):
        """Called by the brain when the pane shows a permission prompt."""
        pid = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        conn.pending = {"id": pid, "info": p, "fut": fut}
        asyncio.create_task(speak_question(conn, pid, permission.summary(p)))  # now, not behind the reply's queue
        await conn.send({"type": "permission", "id": pid, "summary": permission.summary(p), "tool": p["tool"],
                         "detail": p["detail"], "desc": p["desc"], "risky": p["risky"]})
        try:
            answer = await asyncio.wait_for(fut, 300)
        except asyncio.TimeoutError:
            answer = None
        finally:
            conn.pending = None
        await conn.send({"type": "permission_done", "id": pid, "answer": answer})
        if answer == "deny":
            await queue.put("Okay, denied.")  # Claude usually goes quiet after a rejection
        log_event({"permission": p["tool"], "detail": p["detail"], "risky": p["risky"], "answer": answer})
        return answer

    spoken_kinds, counts = set(), {}
    last_sound = [time.time()]  # when anything was last said (a sentence or a cue)

    async def cue_tool(name, inp):
        c = cues.cue(name, inp)
        if not c:
            return
        kind, line = c
        counts[kind] = counts.get(kind, 0) + 1
        await conn.send({"type": "tool", "kind": kind, "count": counts[kind], "line": line, "name": name})
        if kind not in spoken_kinds:
            spoken_kinds.add(kind)
            last_sound[0] = time.time()
            await queue.put(("cue", line))

    async def ticker():
        """Twenty seconds of silence mid-turn gets a "still working"."""
        said = 0
        while said < 3:
            await asyncio.sleep(2)
            if time.time() - last_sound[0] > 20 and not conn.pending:
                last_sound[0] = time.time()
                said += 1
                await queue.put(("cue", "Still working on it."))

    spk = asyncio.create_task(speaker())
    tick = asyncio.create_task(ticker())
    if isinstance(brain, HerdrBrain):
        asyncio.create_task(name_topic(conn, text))
    try:
        async for kind, val in brain.turn(text, ask=ask) if isinstance(brain, HerdrBrain) else brain.turn(text):
            if kind == "text":
                timing.setdefault("first_token", time.time())
                for s in splitter.feed(val):
                    last_sound[0] = time.time()
                    await queue.put(s)
            elif kind == "tool":
                await cue_tool(val["name"], val["input"])
            elif kind == "fallback":
                await conn.send({"type": "history", "items": await brain.history(), "session": brain.session_id})
                await conn.send({"type": "fallback", "name": val, "text": text})
                await send_topics(conn)
                await queue.put(("cue", f"That chat was closed in herdr, so I'm back in {val}."))
            elif kind == "notice":
                await conn.send({"type": "error", "text": val})
                await queue.put(val)
            elif kind == "file":
                files.append(val)
                await conn.send({"type": "file", "path": val})
            elif kind == "done":
                for s in splitter.flush():
                    await queue.put(s)
                timing["result"] = val
                if val.get("error") or val.get("is_error"):
                    await conn.send({"type": "error", "text": f"Claude: {val.get('error') or val.get('result')}"})
    finally:
        tick.cancel()
        await queue.put(None)
        await spk
    timing["done"] = time.time()
    return files


def summarize(timing):
    t0 = timing["end_of_speech"]
    rel = {k: round(v - t0, 2) for k, v in timing.items() if isinstance(v, float) and k != "end_of_speech"}
    return {**rel, "duration_ms": (timing.get("result") or {}).get("duration_ms")}


def log_event(obj):
    with open(LOG, "a") as f:
        f.write(json.dumps({"at": time.time(), "session": brain.session_id, **obj}) + "\n")


async def speak_question(conn, pid, text):
    """Speak a permission question at once; drop it if it was answered while the audio rendered."""
    try:
        audio = await tts.speak(text)
    except Exception:
        audio = None
    if conn.pending and conn.pending["id"] == pid:
        await conn.say(text, -1, audio)


async def answer_pending(conn, answer, by):
    """Resolve the pending permission prompt. Risky prompts only take a tap."""
    p = conn.pending
    if not p or p["fut"].done():
        return False
    if answer == "approve" and p["info"]["risky"] and by != "tap":
        await conn.speak_now("That one needs a tap on the screen.")
        return True
    p["fut"].set_result(answer)
    return True


async def handle_input(conn, text, timing, by):
    """A finished utterance or typed line: a permission answer, or a new turn."""
    if conn.pending:
        answer = permission.answer_of(text)
        if answer:
            await answer_pending(conn, answer, by)
        else:
            await conn.speak_now("Say yes or no.")
        return
    if conn.busy:
        await conn.send({"type": "error", "text": "Still working on the last one."})
        return
    MISSED.clear()  # a new turn supersedes anything left unheard
    conn.turn_task = asyncio.create_task(turn(conn, text, timing))


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)
    conn = Conn(ws)
    authed = not TOKEN
    pending_mime = None
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            m = json.loads(msg.data)
            if m["type"] == "hello":
                authed = authed or m.get("token") == TOKEN
                if not authed:
                    await conn.send({"type": "error", "text": "bad token"})
                    break
                state = await tts.warm()
                asyncio.create_task(brain.ensure())
                await conn.send({"type": "ready", "tts": state, "session": brain.session_id})
                if MISSED:
                    await conn.send({"type": "missed", "text": " ".join(MISSED)})
            elif not authed:
                break
            elif m["type"] == "utterance":
                pending_mime = m.get("mime", "audio/mp4")
            elif m["type"] == "text":
                await handle_input(conn, m["text"], {"end_of_speech": time.time()}, "typed")
            elif m["type"] == "permission_answer":
                if conn.pending and conn.pending["id"] == m.get("id"):
                    await answer_pending(conn, m.get("answer"), "tap")
            elif m["type"] == "new_conversation":
                if conn.busy:
                    await conn.send({"type": "error", "text": "Still working on the last one."})
                    continue
                async with brain.lock:
                    sid = await brain.start(new=True)
                await conn.send({"type": "ready", "tts": await tts.status(), "session": sid})
            elif m["type"] == "replay":
                lines, MISSED[:] = list(MISSED), []
                for i, line in enumerate(lines):
                    try:
                        audio = await tts.speak(line)
                    except Exception:
                        audio = None
                    await conn.say(line, i, audio, replay=True)
            elif m["type"] == "topics":
                await send_topics(conn)
            elif m["type"] in ("switch_topic", "new_topic"):
                if conn.busy:
                    await conn.send({"type": "error", "text": "Still working on the last one."})
                    continue
                try:
                    if m["type"] == "switch_topic":
                        await brain.switch_topic(m["id"])
                    else:
                        async with brain.lock:
                            await brain.new_topic((m.get("name") or "").strip() or "New chat")
                except Exception as e:
                    await conn.send({"type": "error", "text": f"couldn't change topic: {e}"})
                    continue
                items = await brain.history()
                await conn.send({"type": "history", "items": items, "session": brain.session_id})
                await send_topics(conn)
            elif m["type"] == "rename_topic":
                await brain.rename_topic(m["id"], m.get("name") or "Untitled")
                await send_topics(conn)
            elif m["type"] == "history":
                items = await brain.history() if hasattr(brain, "history") else []
                await conn.send({"type": "history", "items": items, "session": brain.session_id})
            elif m["type"] == "status":
                await conn.send({"type": "status", "tts": await tts.status()})
        elif msg.type == WSMsgType.BINARY and authed and pending_mime:
            timing = {"end_of_speech": time.time()}
            pending_mime = None
            try:
                text = await stt.transcribe(msg.data)
            except Exception as e:
                await conn.send({"type": "error", "text": f"speech recognition failed: {e}"})
                await conn.send({"type": "turn_done", "timing": summarize(timing), "empty": True})
                continue
            timing["transcript"] = time.time()
            await conn.send({"type": "transcript", "text": text})
            if text:
                await handle_input(conn, text, timing, "voice")
            elif not conn.pending:
                await conn.send({"type": "turn_done", "timing": summarize(timing), "empty": True})
    if conn.pending and not conn.pending["fut"].done():
        conn.pending["fut"].set_result(None)  # phone went away: leave the prompt for the desk
    return ws


async def name_topic(conn, text):
    try:
        await brain.name_if_untitled(text)
        await send_topics(conn)
    except Exception:
        pass  # naming is cosmetic


async def send_topics(conn):
    items = await brain.topics() if hasattr(brain, "topics") else []
    await conn.send({"type": "topics", "items": items})


async def turn(conn, text, timing):
    try:
        files = await run_turn(conn, text, timing)
    except Exception as e:
        await conn.send({"type": "error", "text": f"turn failed: {e}"})
        files = []
    summary = summarize(timing)
    try:
        await conn.send({"type": "turn_done", "timing": summary, "files": files})
    except Exception:
        pass  # the phone may have gone; the turn still completed in herdr
    log_event({"text": text, "files": files, "timing": summary})
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
