"""The dedicated voice session: one long-lived `claude -p` process in stream-json mode.

Each user turn is written to stdin as a JSON line; text deltas stream back and
are yielded as they arrive, along with any files the agent writes. The session
ID is kept in STATE_DIR so a restart resumes the same conversation.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

STATE = Path(os.environ.get("STATE_DIR", "/state"))
CWD = os.environ.get("BRAIN_CWD", os.path.expanduser("~/Data/knowledge"))
MODEL = os.environ.get("BRAIN_MODEL", "opus")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
TOOLS = "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"
FILE_TOOLS = {"Write", "Edit", "MultiEdit"}


class Brain:
    def __init__(self, contract: str):
        self.contract = contract
        self.proc = None
        self.session_file = STATE / "session.json"
        self.lock = asyncio.Lock()

    def _session(self, new=False):
        if not new and self.session_file.exists():
            return json.loads(self.session_file.read_text())["id"], True
        sid = str(uuid.uuid4())
        STATE.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps({"id": sid}))
        return sid, False

    @property
    def session_id(self):
        return json.loads(self.session_file.read_text())["id"] if self.session_file.exists() else None

    async def start(self, new=False):
        await self.stop()
        sid, resume = self._session(new)
        args = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose", "--model", MODEL,
                "--append-system-prompt", self.contract, "--permission-mode", "acceptEdits",
                "--allowedTools", TOOLS]
        args += ["--resume", sid] if resume else ["--session-id", sid]
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=CWD, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
        asyncio.create_task(self._drain_stderr(self.proc))
        return sid

    async def _drain_stderr(self, proc):
        while line := await proc.stderr.readline():
            print("claude:", line.decode(errors="replace").rstrip(), flush=True)

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None

    async def turn(self, text: str):
        """Yield ("text", delta) and ("file", path) events, then ("done", result)."""
        async with self.lock:
            if not self.proc or self.proc.returncode is not None:
                await self.start()
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    yield "done", {"error": "claude exited"}
                    self.proc = None
                    return
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "stream_event":
                    e = ev["event"]
                    if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                        yield "text", e["delta"]["text"]
                    elif e.get("type") == "content_block_start" and e["content_block"].get("type") == "text":
                        yield "text", "\n"  # a new text block is a sentence boundary
                elif t == "assistant":
                    for block in ev["message"].get("content", []):
                        if block.get("type") == "tool_use" and block.get("name") in FILE_TOOLS:
                            path = block.get("input", {}).get("file_path")
                            if path:
                                yield "file", path
                elif t == "result":
                    yield "done", {"result": ev.get("result", ""), "duration_ms": ev.get("duration_ms"),
                                   "is_error": ev.get("is_error", False)}
                    return
