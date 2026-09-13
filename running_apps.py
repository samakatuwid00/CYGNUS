"""running_apps.py - what is running on this machine, and what it is doing.

RUNNING_APPS_PLAN.md, phases 1-3. inventory() lists apps, agents, servers
and background programs the way Task Manager groups them, including the ones
with no window, and says what each is doing from what it leaves behind: its
command line and ports, Cygnus's task records, a Claude Code session
transcript, Hermes's log, a window title. summary() says it in a short
spoken paragraph, locally, with no model call. It only looks: nothing here
starts, stops or focuses anything.
"""

import datetime
import getpass
import glob
import json
import os
import re
import threading
import time

CACHE_S = 5.0
HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
HERMES_LOG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "logs", "agent.log")
TAIL_BYTES = 64 * 1024
IDLE_S = 600            # an agent quiet this long is idle
STUCK_S = 1800          # a tool call unanswered this long is stuck
WAITING_S = 3600        # a finished reply older than this is idle, not waiting
BUSY_S = 120            # Hermes counts as working this long after a model call or tool
LEFTOVER_S = 86400      # a background program running over a day...
LEFTOVER_CPU = 0.001    # ...that used under 0.1% of that time on the CPU
MAX_SERVERS_SAID = 4
GROUPS = ("agents", "servers", "apps", "background")
_STATUS_ORDER = {"stuck": 0, "waiting": 1, "working": 2, "running": 3, "idle": 4}

# Windows' own processes and app helpers: never a row.
_SKIP = {n.lower() for n in (
    "System", "Idle", "Registry", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "svchost.exe", "winlogon.exe", "dwm.exe", "explorer.exe", "sihost.exe",
    "taskhostw.exe", "ctfmon.exe", "RuntimeBroker.exe", "dllhost.exe", "conhost.exe",
    "OpenConsole.exe", "SearchHost.exe", "StartMenuExperienceHost.exe", "ShellExperienceHost.exe",
    "TextInputHost.exe", "ApplicationFrameHost.exe", "SystemSettings.exe", "backgroundTaskHost.exe",
    "smartscreen.exe", "SecurityHealthSystray.exe", "fontdrvhost.exe", "Widgets.exe",
    "WidgetService.exe", "LockApp.exe", "UserOOBEBroker.exe", "CrossDeviceResume.exe",
    "PhoneExperienceHost.exe", "msedgewebview2.exe", "WmiPrvSE.exe", "chcp.com", "git.exe")}
# Programs that run scripts: worth a row even with no window or port.
_SCRIPT_HOSTS = {"python.exe", "pythonw.exe", "node.exe", "bash.exe", "sh.exe", "powershell.exe",
                 "pwsh.exe", "cmd.exe", "tail.exe", "grep.exe", "java.exe", "ruby.exe", "deno.exe",
                 "bun.exe", "uv.exe"}
# Listening on a port makes these a server; a vendor's background service
# listening on one (a sync client, a driver helper) is not worth a row.
_SERVER_HOSTS = _SCRIPT_HOSTS | {"ollama.exe", "ollama app.exe", "dotnet.exe", "php.exe", "nginx.exe",
                                 "httpd.exe", "caddy.exe", "redis-server.exe", "postgres.exe",
                                 "mysqld.exe", "mongod.exe", "com.docker.backend.exe"}
# Shells and log watchers only mention agents ("tail …/hermes/logs/agent.log").
_NEVER_AGENTS = {"tail.exe", "grep.exe", "bash.exe", "sh.exe", "cmd.exe", "powershell.exe", "pwsh.exe"}
# Programs whose window title names the app better than the program does.
_GENERIC_EXES = {"electron", "python", "pythonw", "java", "javaw", "node"}
# A window titled like an identifier ("Cua.AgentCursorOverlay.default") is an
# overlay or a helper, not an app anyone opened.
_IDENT_TITLE = re.compile(r"^[\w-]+(\.[\w-]+){2,}$")
# Agents that sit open rather than work: named together, briefly.
_QUIET_AGENTS = ("desktop app", "Hermes helper")
_NUMBERS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


# ---------------------------------------------------------------- wording --

def _ago(seconds):
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h = m // 60
    return f"{h} hour{'s' if h != 1 else ''}" if h < 48 else f"{h // 24} days"


def _since(started, now):
    if now - started > 86400:
        return f"since {datetime.datetime.fromtimestamp(started):%A}"
    return f"for {_ago(now - started)}"


def _cap(s):
    return s[:1].upper() + s[1:] if s else s


def _count(n):
    return _NUMBERS.get(n, str(n))


def _join(items):
    """"a, b and c"; with semicolons when an item has its own commas."""
    items = list(items)
    if len(items) < 2:
        return items[0] if items else ""
    if any("," in i for i in items):
        return "; ".join(items[:-1]) + "; and " + items[-1]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _pretty(name):
    """"WindowsTerminal" -> "Windows Terminal"."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def _short_path(p):
    parts = [x for x in re.split(r"[\\/]", p) if x]
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else p


def _short_cmd(proc):
    name = proc["name"].rsplit(".", 1)[0]
    args = [_short_path(a) if ("\\" in a or "/" in a) and len(a) > 30 else a for a in proc["cmdline"][1:]]
    s = " ".join([name] + args)
    return s[:90] + ("…" if len(s) > 90 else "")


def _program(proc):
    """The script or module a host runs ("hermes_cli.main", "…/vite.js"), lowercased."""
    args = proc["cmdline"][1:]
    for i, a in enumerate(args):
        if a == "-m" and i + 1 < len(args):
            return args[i + 1].lower()
        if not a.startswith("-"):
            return a.lower()
    return ""


def _program_label(proc):
    prog = _program(proc)
    base = re.split(r"[\\/]", prog)[-1] if prog else ""
    return (base.rsplit(".", 1)[0] if base.endswith((".js", ".py", ".mjs", ".cjs", ".exe")) else base) \
        or proc["name"].rsplit(".", 1)[0]


# ------------------------------------------------------------ recognising --

def _agent_name(proc):
    """The agent a process is, by the program it runs, or None."""
    name = proc["name"].lower()
    if name == "claude.exe":
        if "windowsapps" in proc["exe"].lower():
            return "Claude desktop app"
        cmd = " ".join(proc["cmdline"]).lower()
        return "Claude one-off run" if re.search(r"(^|\s)(-p|--print)(\s|$)", cmd) else "Claude Code"
    known = {"hermes.exe": "Hermes desktop app", "opencode.exe": "OpenCode", "gemini.exe": "Gemini CLI",
             "codex.exe": "Codex"}
    if name in known:
        return known[name]
    if name in _NEVER_AGENTS:
        return None
    prog = _program(proc)
    if "hermes_cli" in prog:
        return "Hermes gateway" if "gateway" in " ".join(proc["cmdline"]).lower() else "Hermes"
    for word, agent in (("hermes", "Hermes helper"), ("opencode", "OpenCode"),
                        ("gemini", "Gemini CLI"), ("codex", "Codex")):
        if word in prog:
            return agent
    return None


def _is_cygnus_backend(proc):
    return "jarvis_web.py" in " ".join(proc["cmdline"])


def _window_app(proc, titles):
    """(app name, what it shows): "tools.py - jarvis-demo - Visual Studio Code"
    is Visual Studio Code showing "tools.py - jarvis-demo"."""
    title = titles[0]
    if " - " in title:
        head, app = title.rsplit(" - ", 1)
        return app.strip(), head.strip()
    base = proc["name"].rsplit(".", 1)[0]
    if base.lower() in _GENERIC_EXES:
        return title, ""
    return _cap(_pretty(base)), title


def _server_doing(proc, ports):
    cmd = proc["cmdline"]
    port = ", ".join(str(p) for p in ports)
    s = "s" if len(ports) > 1 else ""
    if _is_cygnus_backend(proc):
        return "Cygnus backend", f"Cygnus's own backend on port{s} {port}"
    if "http.server" in cmd:
        folder = next((cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a in ("--directory", "-d")), "")
        where = f" serving {os.path.basename(folder.rstrip(chr(92) + '/')) or folder}" if folder else ""
        return "web server", f"web server on port{s} {port}{where}"
    label = _program_label(proc)
    return _pretty(_cap(label)), f"{label} on port{s} {port}"


# ------------------------------------------------------------ Claude Code --

def _stamp(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _tool_line(use):
    """A Claude tool call in plain words: "editing tools.py"."""
    name = use.get("name") or "a tool"
    inp = use.get("input") or {}
    target = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"editing {target}" if target else "editing a file"
    if name == "Read":
        return f"reading {target}" if target else "reading a file"
    if name in ("Bash", "PowerShell"):
        what = inp.get("description") or str(inp.get("command") or "")[:50]
        return f"running a command: {what}" if what else "running a command"
    if name in ("Grep", "Glob"):
        return f"searching for {str(inp.get('pattern') or '')[:40]}"
    if name in ("Agent", "Task"):
        return f"running a helper agent: {inp.get('description') or ''}".rstrip(": ")
    if name in ("WebSearch", "WebFetch"):
        return "looking something up on the web"
    return f"using {name}"


def claude_session(lines, now):
    """What a Claude Code session is doing, from the tail of its transcript:
    working (a tool call or a prompt it is still answering), waiting for the
    owner (its reply ended), idle, or stuck (a tool call unanswered for 30 min)."""
    title = prompt = None
    last_ts, state, steps = None, None, []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        kind = r.get("type")
        if kind == "ai-title":
            title = r.get("aiTitle") or title
        elif kind == "last-prompt":
            prompt = r.get("lastPrompt") or prompt
        ts = _stamp(r.get("timestamp"))
        if ts:
            last_ts = ts
        content = (r.get("message") or {}).get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if uses:
                steps += [_tool_line(u) for u in uses]
                state = "tool"
            elif any(b.get("type") == "text" for b in blocks):
                state = "replied"
        elif kind == "user" and content and not r.get("isMeta"):
            state = "tool" if any(b.get("type") == "tool_result" for b in blocks) else "prompted"
    age = now - last_ts if last_ts else None
    if age is None:
        status, doing = "idle", "no activity yet"
    elif state == "replied":
        status, doing = ("waiting", "waiting for your reply") if age < WAITING_S else \
            ("idle", f"idle for {_ago(age)}")
    elif state in ("tool", "prompted") and age < STUCK_S:
        status, doing = "working", steps[-1] if state == "tool" and steps else "thinking"
    elif state in ("tool", "prompted"):
        status, doing = "stuck", f"no progress for {_ago(age)}"
    else:
        status, doing = "idle", f"idle for {_ago(age)}"
    detail = ([f"Session: {title}"] if title else []) + ([f"Last prompt: {prompt[:160]}"] if prompt else [])
    detail += [f"Then: {s}" for s in steps[-5:]]
    return {"status": status, "doing": doing, "detail": detail, "title": title}


def _tail(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def _claude_for(proc, now, taken):
    """A Claude Code CLI's session, matched by the folder it runs in: its
    transcript lives in ~/.claude/projects/<folder with every symbol a dash>."""
    try:
        import psutil
        cwd = psutil.Process(proc["pid"]).cwd()
    except Exception:
        return None
    home = os.path.normcase(cwd.rstrip("\\/")) == os.path.normcase(HOME.rstrip("\\/"))
    folder = "your home folder" if home else os.path.basename(cwd.rstrip("\\/")) or cwd
    session_dir = os.path.join(CLAUDE_PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd))
    try:
        files = sorted(glob.glob(os.path.join(session_dir, "*.jsonl")), key=os.path.getmtime, reverse=True)
        files = [f for f in files if f not in taken and os.path.getmtime(f) >= proc["started"] - 60]
    except OSError:
        files = []
    if not files:
        return {"folder": folder, "status": "idle", "doing": "no session activity yet", "detail": []}
    taken.add(files[0])
    return dict(claude_session(_tail(files[0]), now), folder=folder)


# ------------------------------------------------------------------ Hermes --

_HERMES_BUSY = ("API call", "conversation turn", "tool_executor")
_HERMES_DONE = ("Turn ended", "CLI cleanup")


def hermes_status(lines, now):
    """Hermes from its agent.log: working for a while after a model call or
    a tool ran, idle once its turn ended. Other log lines (a messaging
    gateway's housekeeping) do not count as work."""
    last_ts = busy_ts = done_ts = None
    busy_line = done_line = ""
    for line in lines:
        try:
            ts = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        last_ts = ts
        if any(m in line for m in _HERMES_BUSY):
            busy_ts, busy_line = ts, line
        if any(m in line for m in _HERMES_DONE):
            done_ts, done_line = ts, line
    if last_ts is None:
        return {"status": "idle", "doing": "no activity in its log", "detail": []}
    if busy_ts and now - busy_ts < BUSY_S and (done_ts is None or done_ts < busy_ts):
        return {"status": "working", "doing": "working on a task", "detail": [busy_line[:160]]}
    ref = done_ts or busy_ts or last_ts if not done_ts or not busy_ts else max(done_ts, busy_ts)
    return {"status": "idle", "doing": f"idle for {_ago(now - ref)}",
            "detail": [(done_line or busy_line)[:160]] if (done_line or busy_line) else []}


def _hermes_for(now):
    return hermes_status(_tail(HERMES_LOG), now)


# ------------------------------------------------------------------- rows --

def _job_rows(jobs):
    rows = []
    for j in jobs:
        waiting = j.get("state") == "waiting-on-confirm"
        note = (j.get("progress") or [""])[-1] or ""
        rows.append({"id": f"job-{j.get('id')}", "group": "agents", "name": "Cygnus task",
                     "status": "waiting" if waiting else "working",
                     "doing": ("waiting for your confirm: " if waiting else "") + str(j.get("task") or "")[:70],
                     "detail": [note[:160]] if note else [], "flags": [], "since": "", "pids": []})
    return rows


def build_rows(procs, windows, ports, now, jobs=()):
    """Rows from a snapshot: procs {pid: {pid, ppid, name, exe, cmdline,
    started, cpu_s, mem_mb}}, windows {pid: [titles]}, ports {pid: [ports]}.
    A launcher and its child are one row, an agent's shells roll into it (an
    agent it starts keeps a row of its own), and a launcher chain
    ("npm run dev" -> node -> vite) belongs to the server or app it started."""
    windows = {pid: real for pid, ts in windows.items() if (real := [t for t in ts if not _IDENT_TITLE.match(t)])}
    kind = {}
    for pid, p in procs.items():
        if _agent_name(p):
            kind[pid] = "agents"
        elif pid in windows:
            kind[pid] = "apps"
        elif pid in ports and (p["name"].lower() in _SERVER_HOSTS or _is_cygnus_backend(p)):
            kind[pid] = "servers"
        elif p["name"].lower() in _SCRIPT_HOSTS:
            kind[pid] = "background"
    owner = {}

    def owner_of(pid, depth=0):
        if pid in owner:
            return owner[pid]
        p = procs[pid]
        parent = procs.get(p["ppid"])
        own = pid
        if parent is not None and parent["pid"] != pid and depth < 50:
            same = parent["name"].lower() == p["name"].lower()
            if kind.get(pid) == "agents":
                if same and kind.get(parent["pid"]) == "agents":
                    own = owner_of(parent["pid"], depth + 1)
            elif same or kind.get(owner_of(parent["pid"], depth + 1)) in ("agents", "apps", "servers"):
                own = owner_of(parent["pid"], depth + 1)
        owner[pid] = own
        return own

    members = {}
    for pid in procs:
        members.setdefault(owner_of(pid), []).append(pid)

    def group_of(root):
        pids = members[root]
        if kind.get(root) == "agents":
            return "agents"
        if any(m in windows for m in pids):
            return "apps"
        if any(kind.get(m) == "servers" for m in pids):
            return "servers"
        return "background" if kind.get(root) == "background" else None

    children = {}
    for pid, p in procs.items():
        children.setdefault(p["ppid"], []).append(pid)

    def descendants(pid):
        out, stack = [], list(children.get(pid, []))
        while stack and len(out) < 500:
            c = stack.pop()
            out.append(c)
            stack.extend(children.get(c, []))
        return out

    for root in list(members):
        if root not in members or group_of(root) != "background":
            continue
        targets = {owner_of(d) for d in descendants(root)} - {root}
        targets = [t for t in targets if t in members and group_of(t) in ("servers", "apps")]
        if len(targets) == 1:
            members[targets[0]] += members.pop(root)

    def ancestors(pid):
        seen, p = [], procs.get(procs[pid]["ppid"])
        while p is not None and p["pid"] not in seen and len(seen) < 50:
            seen.append(p["pid"])
            p = procs.get(p["ppid"])
        return seen

    claude_roots = sorted((r for r in members if _agent_name(procs[r]) == "Claude Code"),
                          key=lambda r: procs[r]["started"], reverse=True)
    taken, claude = set(), {}
    for r in claude_roots:
        claude[r] = _claude_for(procs[r], now, taken)
    hermes = None

    rows = []
    for root, pids in members.items():
        group = group_of(root)
        if group is None:
            continue
        p = procs[root]
        titles = [t for m in pids for t in windows.get(m, [])]
        status, detail = "running", []
        if group == "agents":
            name = _agent_name(p)
            if name == "Claude Code":
                s = claude.get(root) or {"status": "running", "doing": "session unknown", "detail": []}
                if s.get("folder"):
                    name = f"Claude Code in {s['folder']}"
                status, doing, detail = s["status"], s["doing"], list(s["detail"])
            elif name == "Claude one-off run":
                cmd = p["cmdline"]
                model = next((cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "--model"), "")
                status, doing = "working", f"one-off run{f' on {model}' if model else ''}"
                parent = procs.get(p["ppid"])
                detail = [f"Started by {parent['name']}"] if parent else []
            elif name == "Hermes gateway":
                hermes = hermes or _hermes_for(now)
                status, doing, detail = hermes["status"], hermes["doing"], list(hermes["detail"])
            else:
                doing = titles[0] if titles else "running"
                detail = [] if titles else [_short_cmd(p)]
        elif group == "apps":
            name, doing = _window_app(p, titles)
            detail = [f"Window: {t}" for t in titles[1:6]]
        elif group == "servers":
            server = next((procs[m] for m in pids if kind.get(m) == "servers"), p)
            ports_here = sorted({port for m in pids if kind.get(m) == "servers" for port in ports.get(m, [])})
            name, doing = _server_doing(server, ports_here)
        else:
            name, doing = p["name"].rsplit(".", 1)[0], _short_cmd(p)
        if group in ("apps", "agents") and doing.lower() in name.lower():
            doing = "open"                   # a window titled with its own app's name
        if any(_is_cygnus_backend(procs[a]) for a in ancestors(root)) and not _is_cygnus_backend(p):
            detail.append("Started by Cygnus")
        cpu = sum(procs[m]["cpu_s"] for m in pids)
        mem = sum(procs[m]["mem_mb"] for m in pids)
        since = _since(p["started"], now)
        detail.append(f"Running {since}; CPU {cpu:.0f} s, memory {mem:.0f} MB")
        rows.append({"id": str(root), "group": group, "name": name, "status": status, "doing": doing,
                     "detail": detail, "flags": [], "since": since, "pids": sorted(pids),
                     "uptime": now - p["started"], "cpu_s": cpu,
                     "key": (group, name, " ".join(p["cmdline"][1:]).lower())})

    counts = {}
    for r in rows:
        counts[r["key"]] = counts.get(r["key"], 0) + 1
    for r in rows:
        if r["group"] in ("servers", "background") and counts[r["key"]] > 1:
            r["flags"].append("duplicate")
        if r["group"] == "background" and r["uptime"] > LEFTOVER_S and r["cpu_s"] / r["uptime"] < LEFTOVER_CPU:
            r["flags"].append("leftover")
        if r["status"] == "waiting":
            r["flags"].append("waiting for you")
        if r["status"] == "stuck":
            r["flags"].append("stuck")
        for k in ("key", "uptime", "cpu_s"):
            r.pop(k)
    rows += _job_rows(jobs)
    rows.sort(key=lambda r: (GROUPS.index(r["group"]), _STATUS_ORDER.get(r["status"], 9), r["name"].lower()))
    return rows


# ---------------------------------------------------------------- speaking --

def _phrases(rows):
    """One phrase per kind of row: "two Claude one-off runs, one-off run on haiku"."""
    kinds = {}
    for r in rows:
        kinds.setdefault((r["name"], r["doing"]), []).append(r)
    out = []
    for (name, doing), same in kinds.items():
        if len(same) > 1:
            head, _, tail = name.partition(" in ")
            who = f"{_count(len(same))} {head} sessions in {tail}" if tail else \
                f"{_count(len(same))} {name}{'' if name.endswith('s') else 's'}"
        else:
            who = name
        out.append(f"{who}, {doing}" if doing and doing != "open" else who)
    return out


def summary(rows, focus=None):
    """What is running, as Cygnus says it: the agents that work, the servers,
    what is worth a look, and how many windowed apps. Built from the rows alone."""
    if focus:
        mine = [r for r in rows if focus in r["name"].lower()]
        if not mine:
            return f"I don't see {_cap(focus)} running, sir."
        return _cap(_join(_phrases(mine))) + "."
    agents = [r for r in rows if r["group"] == "agents"]
    busy = [r for r in agents if not any(q in r["name"] for q in _QUIET_AGENTS)]
    quiet = [r for r in agents if r not in busy]
    parts = []
    if busy:
        parts.append("Agents: " + _join(_phrases(busy)) + ".")
    if quiet:
        parts.append("Also open: " + _join(_phrases([dict(r, doing="open") for r in quiet])) + ".")
    served = list(dict.fromkeys(r["doing"] for r in rows if r["group"] == "servers"))
    if served:
        more = len(served) - MAX_SERVERS_SAID
        parts.append("Servers: " + _join(served[:MAX_SERVERS_SAID] + ([f"{more} more"] if more > 0 else [])) + ".")
    look = []
    dups = {}
    for r in rows:
        if "duplicate" in r["flags"]:       # a server by what it serves, anything else by name
            what = f"the {r['doing']}" if r["group"] == "servers" else r["name"]
            dups.setdefault(what, []).append(r)
    look += [f"{_count(len(same))} copies of {what}" for what, same in dups.items()]
    old = [r for r in rows if "leftover" in r["flags"]]
    if len(old) == 1:
        look.append(f"{old[0]['name']} left running {old[0]['since']}")
    elif old:
        oldest = max(old, key=lambda r: len(r["since"]) and r["since"])
        look.append(f"{_count(len(old))} background programs left running, some {oldest['since']}")
    if look:
        parts.append("Worth a look: " + _join(look) + ".")
    apps = [r for r in rows if r["group"] == "apps"]
    if apps:
        names = list(dict.fromkeys(r["name"] for r in apps))
        parts.append(f"Plus {len(apps)} app{'s' if len(apps) != 1 else ''} with windows, like "
                     f"{_join(names[:3])}.")
    return " ".join(parts) or "Nothing of note is running, sir."


def explain_context(rows, focus=None):
    """The agents' state and recent steps, for the answering model to explain."""
    lines = []
    for r in rows:
        if r["group"] != "agents" or (focus and focus not in r["name"].lower()):
            continue
        lines.append(f"{r['name']}: {r['status']}, {r['doing']}")
        lines += [f"  {d}" for d in r["detail"]]
    return "\n".join(lines)


def focus_of(text):
    """The agent a question names, if it names one."""
    t = (text or "").lower()
    return next((w for w in ("claude", "hermes", "opencode", "gemini", "codex") if w in t), None)


# --------------------------------------------------------------- snapshot --

# (pid, start time) -> (exe, command line): they never change for a process,
# and reading them is the slow part (about 4 ms a process on Windows).
_static = {}


def _processes(windows=()):
    """This user's processes. Command lines are read once per process; CPU
    and memory only for the kinds of program that can become a row."""
    import psutil
    me = getpass.getuser().lower()
    procs, seen = {}, set()
    for p in psutil.process_iter(["pid", "ppid", "name", "username", "create_time"], ad_value=None):
        i = p.info
        name = i.get("name") or ""
        if (i.get("username") or "").split("\\")[-1].lower() != me or name.lower() in _SKIP:
            continue
        key = (i["pid"], i.get("create_time") or 0.0)
        seen.add(key)
        if key not in _static:
            try:
                with p.oneshot():
                    _static[key] = (p.exe() or "", p.cmdline() or [])
            except Exception:
                _static[key] = ("", [])
        exe, cmdline = _static[key]
        cpu_s = mem_mb = 0.0
        if name.lower() in _SERVER_HOSTS or name.lower() in ("claude.exe", "hermes.exe") or i["pid"] in windows:
            try:
                with p.oneshot():
                    t = p.cpu_times()
                    cpu_s, mem_mb = t.user + t.system, p.memory_info().rss / 2 ** 20
            except Exception:
                pass
        procs[i["pid"]] = {"pid": i["pid"], "ppid": i.get("ppid"), "name": name, "exe": exe,
                           "cmdline": cmdline, "started": i.get("create_time") or 0.0,
                           "cpu_s": cpu_s, "mem_mb": mem_mb}
    for key in [k for k in _static if k not in seen]:
        del _static[key]
    return procs


def _windows():
    """{pid: [titles]} of the top-level windows the owner can see or restore."""
    import ctypes
    from ctypes import wintypes
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):   # owned popups are not apps
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        n = user32.GetWindowTextLengthW(hwnd)
        if cloaked.value or not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.setdefault(pid.value, []).append(buf.value)
        return True

    user32.EnumWindows(each, 0)
    return found


def _ports():
    import psutil
    out = {}
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.pid:
                out.setdefault(c.pid, set()).add(c.laddr.port)
    except Exception:
        pass
    return {pid: sorted(p) for pid, p in out.items()}


def _cygnus_jobs():
    try:
        import jobs
        return jobs.active()
    except Exception:
        return []


_cache = {"at": 0.0, "rows": []}
_lock = threading.Lock()


def inventory(use_cache=True):
    """The rows for this moment, at most CACHE_S old."""
    with _lock:
        now = time.time()
        if use_cache and now - _cache["at"] < CACHE_S:
            return _cache["rows"]
        windows = _windows()
        rows = build_rows(_processes(windows), windows, _ports(), now, jobs=_cygnus_jobs())
        _cache.update(at=now, rows=rows)
        return rows
