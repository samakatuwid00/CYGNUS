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
# Models that read images, through the local router, tried in order for
# "describe that window": these read a test image on 2026-09-13, when the
# router's default (a Gemini model) was out of quota for three days.
VISION_MODELS = [m.strip() for m in os.getenv("JARVIS_VISION_MODELS",
                                              "cx/gpt-5.6-luna,cx/gpt-5.5,cx/gpt-5.6-terra").split(",") if m.strip()]
TEXT_LINES = 40         # a window's text: this many last lines
SPOKEN_LINES = 6        # of which Cygnus reads out this many
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


def match_transcripts(procs, files):
    """{pid: transcript} for the Claude CLI processes of one folder: each
    takes the transcript created closest after it started, else (a resumed
    session) the one most recently written since it started. procs: [(pid,
    started)], files: [(path, created, modified)]. Newest-modified alone
    gave a 12-hour-old session a 2-minute-old run's work (2026-09-13)."""
    out, taken = {}, set()
    by_age = sorted(procs, key=lambda p: p[1], reverse=True)
    for pid, started in by_age:
        born = [f for f in files if f[0] not in taken and f[1] >= started - 60 and f[2] >= started]
        if born:
            out[pid] = min(born, key=lambda f: f[1])[0]
            taken.add(out[pid])
    for pid, started in by_age:
        if pid in out:
            continue
        active = [f for f in files if f[0] not in taken and f[2] >= started]
        if active:
            out[pid] = max(active, key=lambda f: f[2])[0]
            taken.add(out[pid])
    return out


def _claude_sessions(claude_procs, now):
    """{pid: session} for Claude CLI processes, sessions and one-off runs
    alike, each read from its own transcript in the folder it runs in
    (~/.claude/projects/<folder with every symbol a dash>)."""
    import psutil
    by_folder = {}
    for p in claude_procs:
        try:
            by_folder.setdefault(psutil.Process(p["pid"]).cwd(), []).append(p)
        except Exception:
            continue
    out = {}
    for cwd, ps in by_folder.items():
        home = os.path.normcase(cwd.rstrip("\\/")) == os.path.normcase(HOME.rstrip("\\/"))
        folder = "your home folder" if home else os.path.basename(cwd.rstrip("\\/")) or cwd
        files = []
        for f in glob.glob(os.path.join(CLAUDE_PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd), "*.jsonl")):
            try:
                files.append((f, os.path.getctime(f), os.path.getmtime(f)))
            except OSError:
                pass
        matched = match_transcripts([(p["pid"], p["started"]) for p in ps], files)
        for p in ps:
            if p["pid"] in matched:
                out[p["pid"]] = dict(claude_session(_tail(matched[p["pid"]]), now), folder=folder, matched=True)
            else:
                out[p["pid"]] = {"folder": folder, "status": "idle", "doing": "no session activity yet",
                                 "detail": [], "matched": False}
    return out


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
    # A window is (handle, title); a bare title (tests, older callers) has no handle.
    pairs = {pid: [(None, w) if isinstance(w, str) else tuple(w) for w in ws] for pid, ws in windows.items()}
    pairs = {pid: real for pid, ws in pairs.items() if (real := [(h, t) for h, t in ws if not _IDENT_TITLE.match(t)])}
    windows = {pid: [t for _, t in ws] for pid, ws in pairs.items()}
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

    claude_procs = [procs[r] for r in members if _agent_name(procs[r]) in ("Claude Code", "Claude one-off run")]
    claude = _claude_sessions(claude_procs, now) if claude_procs else {}
    hermes = None

    rows = []
    for root, pids in members.items():
        group = group_of(root)
        if group is None:
            continue
        p = procs[root]
        titles = [t for m in pids for t in windows.get(m, [])]
        status, detail = "running", []
        by_cygnus = any(_is_cygnus_backend(procs[a]) for a in ancestors(root)) and not _is_cygnus_backend(p)
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
                parent = procs.get(p["ppid"])
                detail = [f"Started by {parent['name']}"] if parent and not by_cygnus else []
                s = claude.get(root) or {}
                if s.get("matched"):             # it writes a transcript: say what it is doing
                    name = f"Claude one-off run in {s['folder']}"
                    status, doing = s["status"], s["doing"]
                    detail = ([f"Model: {model}"] if model else []) + detail + list(s["detail"])
                else:
                    status, doing = "working", f"one-off run{f' on {model}' if model else ''}"
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
        if titles and group in ("apps", "agents") and doing.lower() in name.lower():
            doing = "open"                   # a window titled with its own app's name
        if by_cygnus:
            detail.append("Started by Cygnus")
        cpu = sum(procs[m]["cpu_s"] for m in pids)
        mem = sum(procs[m]["mem_mb"] for m in pids)
        since = _since(p["started"], now)
        detail.append(f"Running {since}; CPU {cpu:.0f} s, memory {mem:.0f} MB")
        rows.append({"id": str(root), "group": group, "name": name, "status": status, "doing": doing,
                     "detail": detail, "flags": [], "since": since, "pids": sorted(pids),
                     "windows": [{"hwnd": h, "title": t} for m in pids for h, t in pairs.get(m, []) if h],
                     "exes": sorted({re.split(r"[\\/]", procs[m].get("exe") or procs[m]["name"])[-1].lower()
                                     for m in pids}),
                     "started": p["started"],
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


# ------------------------------------------------ phase 4: seeing more --

# One thread for everything UI Automation. Its COM objects belong to the
# thread that made them; called from another thread they wait for that one
# to answer, and a pool thread never does. Two "/hermes" commands in a row
# on asyncio's pool hung the second for its whole 120 s (2026-09-15).
import concurrent.futures as _cf
import threading as _threading

_UI_THREAD = "ui-automation"


def _ui_init():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


_UI = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=_UI_THREAD, initializer=_ui_init)


def ui_call(fn, *args, **kwargs):
    """Run fn on the UI Automation thread and wait for it (or run it here
    when this already is that thread)."""
    if _threading.current_thread().name.startswith(_UI_THREAD):
        return fn(*args, **kwargs)
    return _UI.submit(fn, *args, **kwargs).result()


def window_text(row, lines=TEXT_LINES):
    return ui_call(_window_text, row, lines)


def _window_text(row, lines=TEXT_LINES):
    """What a row's window shows as text, through Windows UI Automation: a
    terminal's buffer (its TextPattern), else the window's own text labels.
    '' when there is none (a canvas, a video, a window with no UIA text)."""
    if not row or not row.get("windows"):
        return ""
    from pywinauto import Desktop
    from pywinauto.uia_defines import IUIA
    uia = IUIA()
    for w in row["windows"]:
        try:
            found, labels = None, []
            for c in Desktop(backend="uia").window(handle=w["hwnd"]).descendants(depth=8):
                info = c.element_info
                if info.class_name in ("TermControl", "TermControl2") or info.control_type in ("Document", "Edit"):
                    try:
                        pattern = info.element.GetCurrentPattern(uia.UIA_dll.UIA_TextPatternId)
                        text = pattern.QueryInterface(uia.UIA_dll.IUIAutomationTextPattern).DocumentRange.GetText(-1)
                    except Exception:
                        continue
                    if text.strip():
                        found = text
                        break
                elif info.control_type == "Text" and info.name:
                    labels.append(info.name)
            kept = [line.rstrip() for line in (found or "\n".join(labels)).splitlines() if line.strip()]
            if kept:
                return "\n".join(kept[-lines:])
        except Exception:
            continue
    return ""


def capture_window(hwnd, max_side=1280, fmt="PNG"):
    """Image bytes (PNG, or JPEG for the phone's thumbnails) of a window,
    drawn by the window itself (PrintWindow), so it works when other windows
    cover it; None when it cannot be captured."""
    import ctypes
    import io
    from ctypes import wintypes
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    before = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # real pixels on a scaled screen
    try:
        rect = wintypes.RECT()
        if not user32.IsWindow(hwnd) or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        try:
            gdi32.SelectObject(mdc, bmp)
            user32.PrintWindow(hwnd, mdc, 2)                  # PW_RENDERFULLCONTENT

            class BitmapInfo(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
            info = BitmapInfo(ctypes.sizeof(BitmapInfo), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            pixels = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mdc, bmp, 0, h, pixels, ctypes.byref(info), 0)
            image = Image.frombuffer("RGBA", (w, h), pixels, "raw", "BGRA", 0, 1).convert("RGB")
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(before))
    image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    image.save(out, fmt, **({"quality": 70} if fmt == "JPEG" else {}))
    return out.getvalue()


def describe_window(row, question=None):
    """What a row's window shows, in plain words, from a picture of it sent to
    a model that reads images, through the local router (allowed on request).
    (answer, model), or (None, why not)."""
    if not row or not row.get("windows"):
        return None, "it has no window to look at"
    png = capture_window(row["windows"][0]["hwnd"])
    if not png:
        return None, "I couldn't capture that window"
    import base64
    from openai import OpenAI
    from config import ROUTER_API_KEY, ROUTER_BASE_URL
    client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY or "none", timeout=40, max_retries=0)
    ask = question or ("Describe what this window shows and what the program seems to be doing, in two or "
                       "three short spoken sentences. No markup, no lists.")
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}
    for model in VISION_MODELS:
        try:
            reply = client.chat.completions.create(model=model, max_tokens=300, messages=[
                {"role": "user", "content": [{"type": "text", "text": ask}, picture]}])
            text = (reply.choices[0].message.content or "").strip()
        except Exception:
            continue
        if text:
            return text, model
    return None, "no model that reads images answered"


# ------------------------------------------------------- phase 5: acting --

# Words that ask for something rather than name a program.
_REQUEST_WORDS = {
    "stop", "kill", "end", "terminate", "shut", "down", "bring", "put", "pull", "to", "the", "a", "an", "that",
    "this", "those", "these", "please", "cygnus", "front", "forward", "foreground", "show", "me", "window",
    "windows", "switch", "on", "port", "running", "program", "programs", "process", "processes", "read", "what",
    "whats", "is", "in", "describe", "look", "at", "my", "screen", "copies", "copy", "of", "and", "all", "it",
    "them", "duplicate", "duplicates", "leftover", "leftovers", "stuck", "say", "says", "does", "can", "you",
    "for", "sir", "s", "up", "one", "ones", "hey", "display", "onto"}


def find_rows(rows, text):
    """The rows a spoken request names: a process id ("process 1234"), a port
    ("8788"), a flag ("the leftover tail", "the duplicates") or the words of a
    row's name and what it is doing ("the web server", "visual studio code")."""
    t = (text or "").lower()
    m = re.search(r"\bprocess\s+(\d+)\b", t)
    if m:
        return [r for r in rows if int(m.group(1)) in r.get("pids", [])]
    flags = [f for f in ("duplicate", "leftover", "stuck") if f in t]
    pool = [r for r in rows if not flags or any(f in r["flags"] for f in flags)]
    ports = re.findall(r"\b\d{2,5}\b", t)
    if ports:
        hit = [r for r in pool if any(re.search(rf"\b{p}\b", r["doing"]) for p in ports)]
        if hit:
            return hit
    said = [w for w in re.findall(r"[a-z0-9]+", t) if w not in _REQUEST_WORDS]
    if not said:
        return pool if flags else []
    # A row answers to the words of its name, what it is doing, and its
    # group ("server", "app"): "the backend server" is Cygnus's backend,
    # "the web server" the web servers.
    group_word = {"servers": "server", "apps": "app", "agents": "agent", "background": "background"}
    words = {r["id"]: set(re.findall(r"[a-z0-9]+", f"{r['name']} {r['doing']}".lower())) | {group_word[r["group"]]}
             for r in pool}
    has = lambda w, ws: w in ws or (w.endswith("s") and len(w) > 3 and w[:-1] in ws)
    if any(not any(has(w, ws) for ws in words.values()) for w in said):
        return []                       # it names something not running ("the redis server")
    score = {r["id"]: sum(has(w, words[r["id"]]) for w in said) for r in pool}
    best = max(score.values(), default=0)
    return [r for r in pool if best and score[r["id"]] == best]


def _front_row(rows):
    """The row of the window on top that is not Cygnus's own: "my screen"."""
    import ctypes
    from ctypes import wintypes
    by_hwnd = {w["hwnd"]: r for r in rows for w in r.get("windows", []) if r["name"] != "Cygnus"}
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if hwnd in by_hwnd:
            found.append(by_hwnd[hwnd])
            return False
        return True

    ctypes.windll.user32.EnumWindows(each, 0)                  # top of the z-order first
    return found[0] if found else None


def bring_to_front(row):
    """Raise a row's window, restoring it if minimized. (ok, what to say)."""
    import ctypes
    if not row or not row.get("windows"):
        return False, f"{row['name'] if row else 'That'} has no window to bring up, sir."
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = row["windows"][0]["hwnd"]
    if not user32.IsWindow(hwnd):
        return False, f"{row['name']}'s window has closed, sir."
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                            # SW_RESTORE
    # Windows lets only the foreground's own thread raise another window;
    # joining its input queue for a moment makes this process count.
    front_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    mine = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(front_thread, mine, True)
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        user32.AttachThreadInput(front_thread, mine, False)
    return ok, (f"{row['name']} is in front, sir." if ok else f"Windows wouldn't let me bring {row['name']} forward.")


def _identity(pids):
    """[[pid, start time]] of the processes still running, to check at the confirm."""
    import psutil
    out = []
    for pid in pids:
        try:
            out.append([pid, psutil.Process(pid).create_time()])
        except Exception:
            pass
    return out


def _protected():
    """This process and the ones that started it (Cygnus's app, its shell)."""
    import psutil
    me = psutil.Process()
    return {me.pid} | {p.pid for p in me.parents()}


def stop_plan(row):
    """What stopping a row would end: ({what, pids}, None), or (None, why not).
    Only servers and background programs; never Cygnus itself."""
    if not row:
        return None, "it isn't running any more"
    if row["group"] not in ("servers", "background"):
        kind = "an app with a window; close it instead" if row["group"] == "apps" else "an agent"
        return None, f"it is {kind}"
    if row["name"] == "Cygnus backend" or _protected() & set(row.get("pids", [])):
        return None, "it is Cygnus itself"
    pids = _identity(row.get("pids", []))
    if not pids:
        return None, "it has already stopped"
    what = row["doing"] if row["name"].lower() in row["doing"].lower() else f"{row['name']}, {row['doing']}"
    return {"what": f"the {what}" if row["group"] == "servers" else what, "pids": pids}, None


def _alive(plan):
    """The plan's processes that are still the same ones (id and start time)."""
    import psutil
    procs = []
    for pid, started in plan["pids"]:
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - started) < 0.01:
                procs.append(p)
        except Exception:
            pass
    return procs


def stop_now(plan):
    """Stop a confirmed plan: gently, then forcibly after a few seconds.
    A process that ended, or whose id was reused, is left alone."""
    import psutil
    procs = _alive(plan)
    if not procs:
        return f"{_cap(plan['what'])} had already stopped."
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    _, still = psutil.wait_procs(alive, timeout=2) if alive else ([], [])
    if still:
        return f"I couldn't stop all of {plan['what']}: {len(still)} process{'es' if len(still) != 1 else ''} still running."
    return f"Stopped {plan['what']}."


def _stop_request(targets, text):
    """(question, plan) for stopping the targets; with "duplicate" the oldest
    copy of each keeps running."""
    if not targets:
        return "I couldn't tell which program to stop, sir.", None
    if "duplicate" in text.lower():
        oldest = {}
        for r in sorted(targets, key=lambda r: r.get("started", 0)):
            oldest.setdefault((r["group"], r["name"], r["doing"]), r)
        targets = [r for r in targets if oldest[(r["group"], r["name"], r["doing"])] is not r]
        if not targets:
            return "There's only one of those running, sir.", None
    plans, refused = [], []
    for r in targets:
        plan, why = stop_plan(r)
        if plan:
            plans.append(plan)
        else:
            refused.append((r["name"], why))
    if not plans:
        return "I won't stop that, sir: " + _join([f"{n} {why}" for n, why in refused]) + ".", None
    counts = {}                         # "two copies of tail …", not "tail … and tail …"
    for p in plans:
        counts[p["what"]] = counts.get(p["what"], 0) + 1
    merged = {"what": _join([w if n == 1 else f"{_count(n)} copies of {w}" for w, n in counts.items()]),
              "pids": [pid for p in plans for pid in p["pids"]]}
    n = len(merged["pids"])
    question = (f"I'll stop {merged['what']}. That ends {n} process{'es' if n != 1 else ''}. "
                "Say confirm to go ahead, or no to leave it.")
    if refused:
        question = f"I'll leave {_join([n for n, _ in refused])} alone. " + question
    return question, merged


def _speakable(text):
    return ". ".join(line.strip() for line in text.splitlines()[-SPOKEN_LINES:])


_NAME_FILLER = {"microsoft", "ms", "the", "my", "a", "an", "app", "application", "program", "window"}


def app_window(names, exe=None, rows=None):
    """The running app with a window that one of `names` means, or None:
    by its program file ("POWERPNT.EXE") first, then by name ("Microsoft
    PowerPoint" is the "PowerPoint" row). open_application asks this before
    it launches: "show PowerPoint" opened a second PowerPoint (2026-09-14)."""
    rows = inventory() if rows is None else rows
    apps = [r for r in rows if r["group"] == "apps" and r.get("windows")]
    stem = re.split(r"[\\/]", exe)[-1].lower() if exe else ""
    if stem.endswith(".exe"):
        hit = [r for r in apps if stem in r.get("exes", [])]
        if hit:
            return hit[0]
    for name in [names] if isinstance(names, str) else names:
        said = set(re.findall(r"[a-z0-9]+", (name or "").lower())) - _NAME_FILLER
        if not said:
            continue
        for r in apps:
            own = set(re.findall(r"[a-z0-9]+", r["name"].lower())) - _NAME_FILLER
            if own and (own <= said or said <= own):
                return r
    return None


_FRONT_RE = re.compile(
    r"^(?:(?:please|hey|ok(?:ay)?|jarvis|cygnus|can\s+you|could\s+you)[,\s]+)*"
    r"(?:bring\s+up|pull\s+up|switch\s+to|show|display|put|bring|pull)\s+(?:me\s+)?"
    r"(?:on(?:to)?\s+(?:the\s+|my\s+)?screen\s+)?(?:the\s+|my\s+)?(?P<app>.+?)"
    r"(?:\s+window)?(?:\s+(?:(?:on(?:to)?|to)\s+(?:the\s+|my\s+)?(?:screen|front|foreground)|forward|up))?"
    r"(?:\s+(?:for\s+me|please))?\s*[.!?]*$", re.I)


def app_phrase(text):
    """The app a "show X on screen" / "bring X to the front" request names."""
    m = _FRONT_RE.match((text or "").strip())
    return m.group("app").strip() if m else ""


def _front_or_open(rows, text):
    """Bring the window the request names forward; open the app if it is not running."""
    phrase = app_phrase(text)
    windowed = [r for r in find_rows(rows, text) if r.get("windows")]
    row = windowed[0] if windowed else (app_window(phrase, rows=rows) if phrase else None)
    if row:
        return bring_to_front(row)[1]
    if not phrase:
        return "I couldn't tell which window you mean, sir."
    import tools
    out = tools.open_application(phrase)
    if out.startswith("[NEEDS_PICK]"):
        return out.split("] ", 1)[1]
    return out if out.startswith("[") else f"{phrase} wasn't open, so I opened it, sir."


def handle(intent, text, remote=False):
    """A spoken request about what is running: (reply, stop plan to confirm or
    None). From the phone everything but stopping a program."""
    rows = inventory()
    if intent == "running":
        return summary(rows, focus=focus_of(text)), None
    if intent == "running_front":
        return _front_or_open(rows, text), None
    targets = find_rows(rows, text)
    if intent == "running_stop":
        if remote:
            return "From the phone I can't stop programs, sir. Do that from the laptop.", None
        return _stop_request(targets, text)
    windowed = [r for r in targets if r.get("windows")]
    if not targets and intent in ("running_text", "running_describe"):
        front = _front_row(rows)
        windowed = [front] if front else []
    if not windowed:
        return ("I don't see a window for that, sir." if targets else
                "I couldn't tell which window you mean, sir."), None
    row = windowed[0]
    if intent == "running_text":
        seen = window_text(row)
        return (f"{row['name']} shows: {_speakable(seen)}" if seen else
                f"{row['name']} shows no text I can read, sir."), None
    answer, model = describe_window(row)
    if answer is None:
        return f"I couldn't see {row['name']}: {model}.", None
    return f"{answer} I sent a picture of {row['name']} to {model} to see it.", None


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
        found.setdefault(pid.value, []).append((hwnd, buf.value))
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
