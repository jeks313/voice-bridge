"""Claude Code permission dialogs, read off a herdr pane with plain parsing (no model).

The dialog, as Claude Code 2.1 draws it at the bottom of the pane:

    ──────────────────────────────
     Bash command
     Tip: ...
     Create marker file and print done
    ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
     touch /tmp/marker && echo done
    ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
     Do you want to proceed?
     ❯ 1. Yes
       2. Yes, and always allow ...
       4. No
     Esc to cancel · Tab to amend

Approve = Enter with the cursor on "1. Yes"; deny = Esc. The "always allow" and
"switch mode" options are never chosen from here.
"""

import re

OPTION = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+?)\s*$")
RULE = re.compile(r"^[─━]{10,}")
DASH = re.compile(r"^\s*[╌┄]{10,}")

# Never approved by voice: only a deliberate tap on the phone (or the desk).
RISKY = [r"\brm\s+-\w*[rf]", r"\bgit\s+push\b.*(--force|-f\b|--delete)", r"\bgit\s+reset\s+--hard", r"\bgit\s+clean\b",
         r"\bgit\s+branch\s+-D", r"\bkubectl\s+(delete|drain|cordon|replace|scale)\b", r"\bhelm\s+(uninstall|delete)\b",
         r"\bterraform\s+(apply|destroy|import|state)\b", r"\b(DROP|TRUNCATE|DELETE\s+FROM)\b", r"\bmkfs\b", r"\bdd\s+if=",
         r"\b(shutdown|reboot|poweroff)\b", r"\bsystemctl\s+(stop|disable|mask)\b", r"\bsudo\b", r"\bch(mod|own)\s+-R\b",
         r"\b(aws|az|gcloud)\b.*\b(delete|remove|terminate|destroy)\b", r"\bgh\s+(repo\s+delete|release\s+delete)\b",
         r"\bprod(uction)?\b", r"send_email|delete_email|delete_event|delete_contact|delete_note", r"--force\b"]
VERB = {"bash command": "run", "edit file": "edit", "write file": "create", "create file": "create",
        "fetch": "fetch", "web fetch": "fetch", "read file": "read", "notebook edit": "edit"}


def parse(screen: str):
    lines = screen.rstrip("\n").split("\n")
    opts, i = [], len(lines) - 1
    while i >= 0 and not OPTION.match(lines[i]):  # skip the footer
        i -= 1
    wrapped = []
    while i >= 0:
        m = OPTION.match(lines[i])
        if m:
            text = " ".join([m.group(3)] + wrapped)
            opts.insert(0, {"n": int(m.group(2)), "text": text, "cursor": bool(m.group(1))})
            wrapped = []
        elif opts and re.match(r"^\s{5,}\S", lines[i]):
            wrapped.insert(0, lines[i].strip())  # a long option wraps onto indented lines
        else:
            break
        i -= 1
    if not opts or opts[0]["n"] != 1 or opts[0]["text"].lower() != "yes":
        return None
    question = lines[i].strip() if i >= 0 else ""
    start = max((k for k in range(i) if RULE.match(lines[k].strip())), default=-1) + 1
    block = [l.strip() for l in lines[start:i] if l.strip()]
    header = block[0] if block else ""
    body = [l for l in block[1:] if not l.lower().startswith("tip:")]
    # the command or content sits between the dashed rules, when there are any
    dashed = [k for k, l in enumerate(body) if DASH.match(l)]
    if len(dashed) >= 2:
        detail = " ".join(body[dashed[0] + 1:dashed[1]]).strip()
        desc = " ".join(l for l in body[:dashed[0]]).strip()
    else:
        detail = " ".join(l for l in body if not DASH.match(l)).strip()
        desc = ""
    risky = any(re.search(p, f"{header} {detail}", re.I) for p in RISKY)
    return {"tool": header, "desc": desc, "detail": detail, "question": question, "options": opts,
            "cursor_on_yes": opts[0]["cursor"], "risky": risky}


def summary(p) -> str:
    """One spoken line: what it wants to do, and the question."""
    verb = VERB.get(p["tool"].lower(), "use " + p["tool"].lower() if p["tool"] else "do something")
    # the real command when it's short enough to say; Claude's own description of it otherwise
    what = p["detail"] if p["detail"] and len(p["detail"]) <= 70 else (p["desc"] or p["detail"])
    if len(what) > 120:
        what = what[:117].rsplit(" ", 1)[0] + "..."
    tail = " That one needs a tap on the screen." if p["risky"] else " Yes or no?"
    return f"It wants to {verb}: {what}.{tail}"


YES = {"yes", "yeah", "yep", "yup", "approve", "approved", "allow", "sure", "ok", "okay", "go", "proceed", "confirm"}
NO = {"no", "nope", "deny", "denied", "don't", "dont", "stop", "cancel", "reject", "abort"}


def answer_of(transcript: str):
    words = set(re.findall(r"[a-z']+", transcript.lower()))
    phrases = transcript.lower()
    yes = bool(words & YES) or "go ahead" in phrases or "do it" in phrases
    no = bool(words & NO)
    if yes == no:
        return None
    return "approve" if yes else "deny"
