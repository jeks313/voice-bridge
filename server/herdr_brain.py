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

import permission

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


async def herdr_text(*args, timeout=30):
    """For commands that print plain text (agent read)."""
    p = await asyncio.create_subprocess_exec("herdr", *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), timeout)
    if p.returncode:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {err.decode(errors='replace')[:200]}")
    return out.decode(errors="replace")


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class HerdrBrain:
    def __init__(self, contract: str):
        self.lock = asyncio.Lock()
        self.migrate_file = STATE / "session.json"  # the pod-owned session from M1, carried over once
        self.topic_file = STATE / "topic.json"  # the active topic: {"pane": herdr pane id}
        try:
            self.target = json.loads(self.topic_file.read_text())["pane"]
        except (OSError, ValueError, KeyError):
            self.target = NAME  # the original single "voice" agent

    def _set_target(self, pane):
        self.target = pane
        self.topic_file.write_text(json.dumps({"pane": pane}))

    async def agent(self):
        try:
            return (await herdr("agent", "get", self.target))["agent"]
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
        if not a and self.target != NAME:
            self._set_target(NAME)  # the active topic's tab was closed: fall back to the original agent
            self.fell_back = True  # the next turn tells the phone
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
            await herdr("agent", "wait", self.target, "--until", "idle", "--timeout", "60000", timeout=70)
        if carry:
            d = json.loads(self.migrate_file.read_text())
            d["moved_to_herdr"] = True
            self.migrate_file.write_text(json.dumps(d))
        a = await self.agent()
        self._sid = (a.get("agent_session") or {}).get("value")
        return a

    async def history(self, limit=40):
        """Recent turns of the voice session, oldest first: [{"role": "me"|"it", "text", "files"}]."""
        a = await self.agent()
        if not a:
            return []
        path = self._jsonl(a)
        if not path.exists():
            return []
        items = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = ev.get("message") or {}
            if ev.get("type") == "user" and not ev.get("isMeta"):
                c = msg.get("content")
                if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                    continue  # tool output, not something Chris said
                t = text_of(c).strip()
                if not t or t.startswith("<") or t.startswith("/"):
                    continue  # command echoes, caveats, slash commands
                voice = t.startswith("[voice]")
                items.append({"role": "me", "text": t[len("[voice]"):].strip() if voice else t, "voice": voice, "files": []})
            elif ev.get("type") == "assistant":
                for b in msg.get("content", []):
                    if b.get("type") == "text" and b.get("text", "").strip():
                        if items and items[-1]["role"] == "it":
                            items[-1]["text"] += " " + b["text"].strip()
                        else:
                            items.append({"role": "it", "text": b["text"].strip(), "files": []})
                    elif b.get("type") == "tool_use" and b.get("name") in FILE_TOOLS:
                        fp = (b.get("input") or {}).get("file_path")
                        if fp and items:
                            items[-1].setdefault("files", []).append(fp)
        return items[-limit:]

    async def start(self, new=False):
        if new:
            return await self.new_topic()
        await self._ensure()
        return self._sid

    # ---- topics: one herdr tab (with its own claude agent) per conversation, all in the voice workspace

    async def _workspace(self):
        ws = [w for w in (await herdr("workspace", "list"))["workspaces"] if w.get("label") == NAME]
        return ws[0]["workspace_id"] if ws else None

    async def topics(self):
        """Every Claude agent open in herdr: voice-workspace topics first, then the rest, each most recent first.
        [{"id": pane, "tab", "name", "group": "voice"|"other", "workspace", "status", "last", "active"}]"""
        wss = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in (await herdr("workspace", "list"))["workspaces"]}
        wid = next((k for k, v in wss.items() if v == NAME), None)
        agents = [a for a in (await herdr("agent", "list"))["agents"] if a.get("agent") == "claude"]
        tabs = {}
        for w in {a["workspace_id"] for a in agents}:
            for t in (await herdr("tab", "list", "--workspace", w))["tabs"]:
                tabs[t["tab_id"]] = t
        out = []
        for a in agents:
            voice = a["workspace_id"] == wid
            label = (tabs.get(a["tab_id"]) or {}).get("label") or ""
            title = (a.get("terminal_title_stripped") or "").strip()
            if voice:
                name = label or "Untitled"
            else:  # desk sessions: Claude's own session title beats herdr's numeric tab label
                name = title if title and title != "Claude Code" else (label if label and not label.isdigit() else f"Tab {label or '?'}")
            path = self._jsonl(a)
            out.append({"id": a["pane_id"], "tab": a["tab_id"], "name": name, "group": "voice" if voice else "other",
                        "workspace": wss.get(a["workspace_id"], a["workspace_id"]), "status": a.get("agent_status"),
                        "last": path.stat().st_mtime if path.exists() else 0,
                        "active": a["pane_id"] == self.target or a.get("name") == self.target})
        return sorted(out, key=lambda t: (t["group"] != "voice", -t["last"]))

    async def switch_topic(self, pane):
        async with self.lock:
            if not any(t["id"] == pane for t in await self.topics()):
                raise HerdrError("no such voice topic")
            self._set_target(pane)
            return await self._ensure()

    async def new_topic(self, name="New chat"):
        """A fresh tab in the voice workspace running its own claude agent; it becomes the active topic."""
        await self._ensure()  # makes the workspace on first use
        wid = await self._workspace()
        r = await herdr("tab", "create", "--workspace", wid, "--cwd", CWD, "--label", name, "--no-focus")
        pane = r["root_pane"]["pane_id"]
        await asyncio.sleep(1.5)  # let the shell reach its prompt
        agent_name = "voice-" + pane.replace(":", "-").lower()
        try:
            await herdr("agent", "start", agent_name, "--kind", "claude", "--pane", pane, "--timeout", "90000",
                        "--", "--model", MODEL, timeout=120)
        except HerdrError as e:
            if "agent_not_ready" not in str(e):
                raise
            await herdr("agent", "wait", pane, "--until", "idle", "--timeout", "60000", timeout=70)
        self._set_target(pane)
        a = await self.agent()
        self._sid = (a.get("agent_session") or {}).get("value")
        return self._sid

    async def rename_topic(self, pane, name):
        t = [t for t in await self.topics() if t["id"] == pane and t["group"] == "voice"]
        if t:
            await herdr("tab", "rename", t[0]["tab"], *name.split())

    async def name_if_untitled(self, text):
        """Name a "New chat" tab after the first thing said in it."""
        t = [t for t in await self.topics() if t["active"]]
        if t and t[0]["group"] == "voice" and t[0]["name"] == "New chat":
            words = [w.strip(".,?!") for w in text.split()][:5]
            if words:
                await herdr("tab", "rename", t[0]["tab"], *words)

    async def stop(self):
        pass  # the pane belongs to herdr; it outlives the bridge

    async def _wait_ready(self, timeout_s=600):
        a = await self.agent()
        if a and a.get("agent_status") in READY:
            return a
        # queue behind whatever the pane is doing (e.g. Chris typing at his desk)
        await herdr("agent", "wait", self.target, "--until", "idle", "--until", "done", "--until", "blocked",
                    "--timeout", str(timeout_s * 1000), timeout=timeout_s + 10)
        return await self.agent()

    def _jsonl(self, a):
        sid = (a.get("agent_session") or {}).get("value")
        found = list(PROJECTS.glob(f"*/{sid}.jsonl")) if sid else []
        if found:
            return found[0]
        return PROJECTS / ("-" + a["cwd"].strip("/").replace("/", "-").replace(".", "-")) / f"{sid}.jsonl"

    async def answer_permission(self, ask):
        """The pane is blocked: read the dialog, ask Chris, press the answer. Returns what happened."""
        screen = await herdr_text("agent", "read", self.target, "--source", "visible", "--lines", "80")
        p = permission.parse(screen)
        if not p:
            return "unreadable"
        answer = await ask(p)
        if answer == "approve":
            if not p["cursor_on_yes"]:
                return "cursor not on Yes; left for the desk"
            await herdr("agent", "send-keys", self.target, "Enter")
        elif answer == "deny":
            await herdr("agent", "send-keys", self.target, "esc")
        else:
            return "no answer; left for the desk"
        for _ in range(40):  # the dialog takes a moment to clear
            await asyncio.sleep(0.25)
            if (await self.agent() or {}).get("agent_status") != "blocked":
                break
        return answer

    async def turn(self, text: str, ask=None):
        """Yield ("text", block), ("file", path), ("notice", msg) and finally ("done", info)."""
        async with self.lock:
            a = await self._ensure()
            if getattr(self, "fell_back", False):
                self.fell_back = False
                cur = [t for t in await self.topics() if t["active"]]
                yield "fallback", cur[0]["name"] if cur else NAME
            a = await self._wait_ready()
            if a.get("agent_status") == "blocked":
                yield "notice", "The voice session is waiting on a permission prompt in herdr."
                yield "done", {"error": "agent blocked"}
                return
            path = self._jsonl(a)
            offset = path.stat().st_size if path.exists() else 0
            prompt = "[voice] " + " ".join(text.split())
            await herdr("agent", "prompt", self.target, prompt)
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
                            elif block.get("type") == "tool_use":
                                yield "tool", {"name": block.get("name"), "input": block.get("input") or {}}
                                if block.get("name") in FILE_TOOLS and (fp := (block.get("input") or {}).get("file_path")):
                                    yield "file", fp
                if new:
                    quiet_since = None
                    continue
                await asyncio.sleep(0.15)
                status = (await self.agent() or {}).get("agent_status")
                if status != "blocked":
                    told_blocked = False  # a later prompt in this turn gets asked again
                elif ask and not told_blocked:
                    result = await self.answer_permission(ask)
                    if result not in ("approve", "deny"):
                        told_blocked = True  # say it once, then leave it for the desk
                        yield "notice", f"Permission prompt {result}."
                elif not told_blocked:
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
