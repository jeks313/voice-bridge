"""The voice session as a real herdr pane on spiff (M4 relay).

Turns are typed into a herdr Claude agent named "voice" (prefixed "[voice]", so the
user-level UserPromptSubmit hook adds the spoken contract), and replies are read
from that session's JSONL as each content block lands. The pane is a normal
interactive session: host environment, MCP servers, SSH agent, hooks, auto mode,
visible in herdr and Collie.

Same interface as brain.Brain: ensure(), start(new), turn(text), session_id.
"""

import asyncio
import json
import os
from pathlib import Path

NAME = os.environ.get("HERDR_AGENT", "voice")
CWD = os.environ.get("BRAIN_CWD", os.path.expanduser("~/Data/knowledge"))
MODEL = os.environ.get("BRAIN_MODEL", "opus")
STATE = Path(os.environ.get("STATE_DIR", "/state"))
PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
FILE_TOOLS = {"Write", "Edit", "MultiEdit"}
READY = {"idle", "done"}


class HerdrError(RuntimeError):
    pass


async def herdr(*args, timeout=90):
    p = await asyncio.create_subprocess_exec("herdr", *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), timeout)
    try:
        d = json.loads(out.strip() or err.strip() or b"{}")  # errors come back on stderr
    except json.JSONDecodeError:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {(out or err).decode(errors='replace')[:200]}")
    if "error" in d:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {d['error'].get('code')}: {d['error'].get('message')}")
    return d.get("result", {})


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class HerdrBrain:
    def __init__(self, contract: str):
        self.lock = asyncio.Lock()
        self.migrate_file = STATE / "session.json"  # the pod-owned session from M1, carried over once

    async def agent(self):
        try:
            return (await herdr("agent", "get", NAME))["agent"]
        except HerdrError:
            return None

    @property
    def session_id(self):
        return getattr(self, "_sid", None)

    async def _carry_over_id(self):
        """The M1 pod session, if it has history and hasn't been moved into herdr yet."""
        if not self.migrate_file.exists():
            return None
        d = json.loads(self.migrate_file.read_text())
        if d.get("moved_to_herdr"):
            return None
        sid = d["id"]
        if not any(PROJECTS.glob(f"*/{sid}.jsonl")):
            return None
        return sid

    async def ensure(self):
        async with self.lock:
            return await self._ensure()

    async def _ensure(self):
        a = await self.agent()
        if a:
            self._sid = (a.get("agent_session") or {}).get("value")
            return a
        # find or make the "voice" workspace; its root pane hosts the agent
        ws = [w for w in (await herdr("workspace", "list"))["workspaces"] if w.get("label") == NAME]
        if ws:
            panes = (await herdr("pane", "list", "--workspace", ws[0]["workspace_id"]))["panes"]
            pane = panes[0]["pane_id"]
        else:
            pane = (await herdr("workspace", "create", "--cwd", CWD, "--label", NAME, "--no-focus"))["root_pane"]["pane_id"]
            await asyncio.sleep(1.5)  # let the shell reach its prompt
        args = ["agent", "start", NAME, "--kind", "claude", "--pane", pane, "--timeout", "90000", "--",
                "--model", MODEL]
        carry = await self._carry_over_id()
        if carry:
            args += ["--resume", carry]
        try:
            await herdr(*args, timeout=120)
        except HerdrError as e:
            if "agent_not_ready" not in str(e):
                raise
            await herdr("agent", "wait", NAME, "--until", "idle", "--timeout", "60000", timeout=70)
        if carry:
            d = json.loads(self.migrate_file.read_text())
            d["moved_to_herdr"] = True
            self.migrate_file.write_text(json.dumps(d))
        a = await self.agent()
        self._sid = (a.get("agent_session") or {}).get("value")
        return a

    async def start(self, new=False):
        a = await self._ensure()
        if new:
            await self._wait_ready()
            await herdr("agent", "prompt", NAME, "/clear")
            await asyncio.sleep(1.0)
            a = await self.agent()
            self._sid = (a.get("agent_session") or {}).get("value")
        return self._sid

    async def stop(self):
        pass  # the pane belongs to herdr; it outlives the bridge

    async def _wait_ready(self, timeout_s=600):
        a = await self.agent()
        if a and a.get("agent_status") in READY:
            return a
        # queue behind whatever the pane is doing (e.g. Chris typing at his desk)
        await herdr("agent", "wait", NAME, "--until", "idle", "--until", "done", "--until", "blocked",
                    "--timeout", str(timeout_s * 1000), timeout=timeout_s + 10)
        return await self.agent()

    def _jsonl(self, a):
        sid = (a.get("agent_session") or {}).get("value")
        found = list(PROJECTS.glob(f"*/{sid}.jsonl")) if sid else []
        if found:
            return found[0]
        return PROJECTS / ("-" + a["cwd"].strip("/").replace("/", "-").replace(".", "-")) / f"{sid}.jsonl"

    async def turn(self, text: str):
        """Yield ("text", block), ("file", path), ("notice", msg) and finally ("done", info)."""
        async with self.lock:
            a = await self._ensure()
            a = await self._wait_ready()
            if a.get("agent_status") == "blocked":
                yield "notice", "The voice session is waiting on a permission prompt in herdr."
                yield "done", {"error": "agent blocked"}
                return
            path = self._jsonl(a)
            offset = path.stat().st_size if path.exists() else 0
            prompt = "[voice] " + " ".join(text.split())
            await herdr("agent", "prompt", NAME, prompt)
            loop = asyncio.get_running_loop()
            started = loop.time()
            seen_prompt = said = False
            buf = b""
            quiet_since = None
            told_blocked = False
            while True:
                # the session can switch files (/clear, resume); follow the agent's current one
                if not path.exists():
                    a = await self.agent()
                    path = self._jsonl(a)
                    offset = 0
                new = b""
                if path.exists():
                    with open(path, "rb") as f:
                        f.seek(offset)
                        new = f.read()
                    offset += len(new)
                buf += new
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = ev.get("message") or {}
                    if ev.get("type") == "user" and not seen_prompt and text_of(msg.get("content")).startswith("[voice]"):
                        seen_prompt = True
                    elif ev.get("type") == "assistant" and seen_prompt:
                        for block in msg.get("content", []):
                            if block.get("type") == "text" and block.get("text", "").strip():
                                said = True
                                yield "text", block["text"] + "\n"
                            elif block.get("type") == "tool_use" and block.get("name") in FILE_TOOLS:
                                if fp := (block.get("input") or {}).get("file_path"):
                                    yield "file", fp
                if new:
                    quiet_since = None
                    continue
                await asyncio.sleep(0.15)
                status = (await self.agent() or {}).get("agent_status")
                if status == "blocked" and not told_blocked:
                    told_blocked = True
                    yield "notice", "It's waiting for your permission in the herdr pane."
                if status in READY and seen_prompt and (said or loop.time() - started > 5):
                    quiet_since = quiet_since or loop.time()
                    if loop.time() - quiet_since > 0.4:  # settled: no new lines after the agent went idle
                        yield "done", {"duration_ms": int((loop.time() - started) * 1000)}
                        return
                if loop.time() - started > 900:
                    yield "done", {"error": "timed out waiting for the voice session"}
                    return
