"""Dev client: drive the voice server like the phone would. Usage: devclient.py URL [audio-file | text]..."""
import asyncio, json, sys, time, aiohttp, mimetypes, os

async def main(url, turns):
    out = os.environ.get("OUT", "/tmp")
    async with aiohttp.ClientSession() as s, s.ws_connect(url, max_msg_size=64 << 20) as ws:
        await ws.send_json({"type": "hello", "token": os.environ.get("VOICE_TOKEN", "")})
        print("<", (await ws.receive_json()))
        n = 0
        for t in turns:
            t0 = time.time()
            if os.path.isfile(t):
                await ws.send_json({"type": "utterance", "mime": mimetypes.guess_type(t)[0] or "audio/webm"})
                await ws.send_bytes(open(t, "rb").read())
            else:
                await ws.send_json({"type": "text", "text": t})
            while True:
                m = await ws.receive()
                if m.type == aiohttp.WSMsgType.BINARY:
                    n += 1; open(f"{out}/reply{n}.wav", "wb").write(m.data)
                    print(f"  {time.time()-t0:5.2f}s audio {len(m.data)//1024} KB -> reply{n}.wav")
                    continue
                d = json.loads(m.data)
                print(f"  {time.time()-t0:5.2f}s {d['type']}: {d.get('text') or d.get('path') or d.get('timing') or ''}")
                if d["type"] in ("turn_done",) or (d["type"] == "error" and "turn failed" in d.get("text", "")):
                    break

asyncio.run(main(sys.argv[1], sys.argv[2:]))
