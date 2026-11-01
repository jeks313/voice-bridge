"""Short spoken cues for tool calls, worked out from the tool name (no model).

The silence while the agent researches is what makes a voice turn feel stuck,
so the bridge says what kind of work is happening. One cue per kind of tool
per turn; repeats only update the on-screen status.
"""

import re


def cue(name: str, inp: dict):
    """(kind, spoken line), or None for internal plumbing that isn't worth saying."""
    n = name or ""
    inp = inp or {}
    if n in ("ToolSearch", "TodoWrite", "TaskStop", "ListMcpResourcesTool"):
        return None
    if n.startswith("mcp__reddit__"):
        return "reddit", "Checking Reddit."
    if n in ("WebSearch",) or "web_search" in n:
        return "search", "Searching the web."
    if n in ("WebFetch",) or "fetch_page" in n:
        return "fetch", "Reading a web page."
    if n.startswith("mcp__fastmail__"):
        if re.search(r"event|calendar|rsvp", n):
            return "calendar", "Checking your calendar."
        if re.search(r"contact", n):
            return "contacts", "Looking up contacts."
        if re.search(r"note", n):
            return "notes", "Checking your notes."
        return "email", "Checking your email."
    if "Claude_Docs" in n or n.startswith("mcp__claude-artifacts__"):
        return "docs", "Checking your Claude docs."
    if n in ("Read", "Glob", "Grep"):
        path = str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or "")
        return ("vault", "Looking in the vault.") if "knowledge" in path or not path.startswith("/") else ("files", "Looking through the files.")
    if n in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "write", "Writing that down."
    if n in ("Agent", "Task"):
        return "agent", "Handing part of that to a helper."
    if n == "Skill":
        return "skill", "Using one of your skills."
    if n == "Bash":
        cmd = str(inp.get("command", ""))
        first = cmd.strip().split()[0] if cmd.strip() else ""
        if re.match(r"(sudo\s+)?kubectl\b", cmd.strip()):
            return "cluster", "Checking the cluster."
        if first == "git" or " git " in f" {cmd} ":
            return "git", "Running git."
        if first in ("ssh", "scp"):
            host = next((w.split("@")[-1] for w in cmd.split()[1:] if not w.startswith("-")), "")
            return "ssh", f"Connecting to {host}." if host else "Connecting to another machine."
        if first in ("herdr",):
            return "herdr", "Checking herdr."
        if first in ("curl", "wget"):
            return "http", "Calling a web service."
        return "shell", "Running a command."
    m = re.match(r"mcp__([^_]+(?:[-_][^_]+)*?)__", n)
    if m:
        return m.group(1), f"Using {m.group(1).replace('-', ' ').replace('_', ' ')}."
    return n.lower(), f"Using {n}."


LABEL = {"reddit": "Reddit", "search": "web search", "fetch": "web page", "email": "email", "calendar": "calendar",
         "vault": "vault", "files": "files", "write": "writing", "cluster": "kubectl", "git": "git", "shell": "shell"}
