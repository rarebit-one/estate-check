#!/usr/bin/env python3
"""
estate-sync — render a repo's per-vendor agent files from its canonical ones.
Stdlib only (Python 3.11+ for tomllib), so it runs anywhere estate-check does.

  estate-sync.py [PATH]                    # write the rendered files
  estate-sync.py --check [PATH]            # report drift, write nothing (exit 1 on drift)
  estate-sync.py --estate DIR [PATH]       # also refresh vendored estate hooks from DIR
                                           #   (an agent-estate checkout; records its HEAD)
  estate-sync.py --init --estate DIR [PATH]  # write .agents/hooks.toml + estate.lock from
                                           #   today's .claude/settings.json registrations

Canonical (hand-written):
  .agents/estate.lock    TOML: org, targets, estate {repo, sha}, [vendored] id = "sha256:…"
  .agents/hooks.toml     TOML: [[hook]] id, run?, timeout?, events = [{on, tools?}]
  .agents/skills/        skills, one directory each

Rendered (committed; `--check` fails when they drift):
  .claude/settings.json  only the keys it owns: `hooks`, and `env.CLAUDE_WS_ORG` when the
                         lock sets `org`. Every other key (permissions, plugins, …) is kept.
  .agents/hooks/<id>.sh  estate hooks, vendored; their sha256 must match the lock
  .claude/skills         a symlink to ../.agents/skills

A hook with `run` is repo-local and runs from that path. A hook without `run` is an
estate hook, vendored at .agents/hooks/<id>.sh.

Events: session_start, pre_tool, post_tool, stop.
Tool classes (pre_tool / post_tool only): edit, write, notebook, bash.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

LOCK = ".agents/estate.lock"
HOOKS = ".agents/hooks.toml"
VENDOR_DIR = ".agents/hooks"
SETTINGS = ".claude/settings.json"
SKILLS, CLAUDE_SKILLS, SKILLS_LINK = ".agents/skills", ".claude/skills", "../.agents/skills"

TARGETS = {"claude"}  # codex, opencode, qwen: added once each vendor's loading is verified
CLAUDE_EVENTS = {"session_start": "SessionStart", "pre_tool": "PreToolUse", "post_tool": "PostToolUse", "stop": "Stop"}
TOOL_EVENTS = {"pre_tool", "post_tool"}
CLAUDE_TOOLS = {"edit": "Edit", "write": "Write", "notebook": "NotebookEdit", "bash": "Bash"}
PROJECT_DIR = "$CLAUDE_PROJECT_DIR/"


class SpecError(Exception):
    pass


def finding(rule, sev, path, line, msg):
    return {"rule": rule, "severity": sev, "file": str(path), "line": line, "message": msg}


def sha256(p):
    return "sha256:" + hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---------------------------------------------------------------- canonical specs

def load_toml(root, rel):
    if tomllib is None:
        raise SpecError("estate-sync needs Python 3.11+ (tomllib)")
    try:
        with open(root / rel, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise SpecError(f"{rel} is missing")
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"{rel}: {e}")


def load_lock(root):
    lock = load_toml(root, LOCK)
    if lock.get("version") != 1:
        raise SpecError(f"{LOCK}: version must be 1")
    targets = lock.get("targets", ["claude"])
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise SpecError(f"{LOCK}: unsupported target(s) {unknown}; supported: {sorted(TARGETS)}")
    org = lock.get("org")
    if org is not None and not isinstance(org, str):
        raise SpecError(f"{LOCK}: org must be a string")
    vendored = lock.get("vendored", {})
    if not isinstance(vendored, dict):
        raise SpecError(f"{LOCK}: [vendored] must be a table")
    return {"org": org, "targets": targets, "estate": lock.get("estate", {}), "vendored": dict(vendored)}


def load_hooks(root):
    if not (root / HOOKS).exists():
        return []
    spec = load_toml(root, HOOKS)
    if spec.get("version") != 1:
        raise SpecError(f"{HOOKS}: version must be 1")
    hooks, seen = [], set()
    for i, h in enumerate(spec.get("hook", [])):
        where = f"{HOOKS}: hook #{i + 1}"
        hid = h.get("id")
        if not isinstance(hid, str) or not hid or "/" in hid:
            raise SpecError(f"{where}: `id` must be a plain name")
        if hid in seen:
            raise SpecError(f"{where}: duplicate id {hid!r}")
        seen.add(hid)
        run = h.get("run")
        if run is not None and (not isinstance(run, str) or run.startswith("/") or ".." in Path(run).parts):
            raise SpecError(f"{where} ({hid}): `run` must be a repo-relative path")
        timeout = h.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            raise SpecError(f"{where} ({hid}): `timeout` must be a positive integer")
        events = h.get("events")
        if not isinstance(events, list) or not events:
            raise SpecError(f"{where} ({hid}): `events` must be a non-empty list")
        for ev in events:
            on, tools = ev.get("on"), ev.get("tools")
            if on not in CLAUDE_EVENTS:
                raise SpecError(f"{where} ({hid}): unknown event {on!r}; one of {sorted(CLAUDE_EVENTS)}")
            if on in TOOL_EVENTS:
                if not isinstance(tools, list) or not tools:
                    raise SpecError(f"{where} ({hid}): `{on}` needs a non-empty `tools` list")
                bad = [t for t in tools if t not in CLAUDE_TOOLS]
                if bad:
                    raise SpecError(f"{where} ({hid}): unknown tool class(es) {bad}; one of {sorted(CLAUDE_TOOLS)}")
            elif tools is not None:
                raise SpecError(f"{where} ({hid}): `{on}` takes no `tools`")
        hooks.append({"id": hid, "run": run, "timeout": timeout, "events": events})
    return hooks


def hook_path(h):
    return h["run"] or f"{VENDOR_DIR}/{h['id']}.sh"


# ---------------------------------------------------------------- rendering

def render_claude_hooks(hooks):
    """The `hooks` value for .claude/settings.json: one group per (event, matcher), in first-seen order."""
    out = {}
    for h in hooks:
        entry = {"type": "command", "command": PROJECT_DIR + hook_path(h)}
        if h["timeout"]:
            entry["timeout"] = h["timeout"]
        for ev in h["events"]:
            groups = out.setdefault(CLAUDE_EVENTS[ev["on"]], [])
            matcher = "|".join(CLAUDE_TOOLS[t] for t in ev["tools"]) if ev["on"] in TOOL_EVENTS else None
            group = next((g for g in groups if g.get("matcher") == matcher), None)
            if group is None:
                group = ({"matcher": matcher} if matcher is not None else {}) | {"hooks": []}
                groups.append(group)
            group["hooks"].append(dict(entry))
    return out


def render_settings(current, lock, hooks):
    """`current` with the owned keys replaced. Key order of everything else is preserved."""
    s = dict(current)
    rendered = render_claude_hooks(hooks)
    if rendered:
        s["hooks"] = rendered
    else:
        s.pop("hooks", None)
    env = dict(s.get("env") or {})
    if lock["org"]:
        env["CLAUDE_WS_ORG"] = lock["org"]
    if env:
        s["env"] = env
    return s


def dump_json(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def q(v):
    return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def dump_lock(lock):
    lines = ["# Rendered-file lock for estate-sync. Edit org/targets by hand;", "# `estate-sync --estate DIR` maintains [estate] and [vendored].", "version = 1"]
    if lock["org"]:
        lines.append(f"org = {q(lock['org'])}")
    lines.append("targets = [" + ", ".join(q(t) for t in lock["targets"]) + "]")
    if lock["estate"]:
        lines += ["", "[estate]"] + [f"{k} = {q(v)}" for k, v in lock["estate"].items()]
    lines += ["", "[vendored]"] + [f"{q(k)} = {q(v)}" for k, v in sorted(lock["vendored"].items())]
    return "\n".join(lines) + "\n"


def dump_hooks(hooks):
    lines = ["# Canonical hook map, rendered per vendor by estate-sync.", "# No `run` = an estate hook, vendored at .agents/hooks/<id>.sh.", "version = 1"]
    for h in hooks:
        lines += ["", "[[hook]]", f"id = {q(h['id'])}"]
        if h["run"]:
            lines.append(f"run = {q(h['run'])}")
        if h["timeout"]:
            lines.append(f"timeout = {h['timeout']}")
        evs = []
        for ev in h["events"]:
            parts = [f"on = {q(ev['on'])}"]
            if ev.get("tools"):
                parts.append("tools = [" + ", ".join(q(t) for t in ev["tools"]) + "]")
            evs.append("{ " + ", ".join(parts) + " }")
        lines.append("events = [" + ", ".join(evs) + "]")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- check

def check(root):
    """Drift findings for a repo that has opted in (has .agents/estate.lock); [] otherwise."""
    root = Path(root)
    if not (root / LOCK).exists():
        return []
    try:
        lock, hooks = load_lock(root), load_hooks(root)
    except SpecError as e:
        return [finding("sync.spec", "error", LOCK if LOCK in str(e) else HOOKS, None, str(e))]
    out = []

    # vendored estate hooks: present, executable, hash-locked; nothing stale
    estate_ids = {h["id"] for h in hooks if not h["run"]}
    for hid in sorted(estate_ids):
        rel = f"{VENDOR_DIR}/{hid}.sh"
        p = root / rel
        if not p.is_file():
            out.append(finding("sync.vendored", "error", rel, None, "estate hook is missing; run `estate-sync --estate <agent-estate>`"))
            continue
        want = lock["vendored"].get(hid)
        if want is None:
            out.append(finding("sync.vendored", "error", LOCK, None, f"no [vendored] hash for {hid!r}"))
        elif sha256(p) != want:
            out.append(finding("sync.vendored", "error", rel, None, f"content differs from the hash locked in {LOCK}; edit the hook in agent-estate, not here"))
        if not os.access(p, os.X_OK):
            out.append(finding("sync.vendored", "error", rel, None, "not executable"))
    for hid in sorted(set(lock["vendored"]) - estate_ids):
        out.append(finding("sync.vendored", "error", LOCK, None, f"[vendored] lists {hid!r}, which no hook uses"))
    for h in hooks:
        if h["run"] and not (root / h["run"]).is_file():
            out.append(finding("sync.spec", "error", HOOKS, None, f"{h['id']}: `run` path {h['run']} does not exist"))

    # .claude/settings.json owned keys
    if "claude" in lock["targets"]:
        try:
            current = json.loads((root / SETTINGS).read_text()) if (root / SETTINGS).exists() else {}
        except json.JSONDecodeError as e:
            current = None
            out.append(finding("sync.drift", "error", SETTINGS, None, f"not valid JSON: {e}"))
        if current is not None:
            want = render_settings(current, lock, hooks)
            if current.get("hooks") != want.get("hooks"):
                out.append(finding("sync.drift", "error", SETTINGS, None, f"`hooks` differs from {HOOKS}; run `estate-sync` and commit the result"))
            if (current.get("env") or {}).get("CLAUDE_WS_ORG") != (want.get("env") or {}).get("CLAUDE_WS_ORG"):
                out.append(finding("sync.drift", "error", SETTINGS, None, f"`env.CLAUDE_WS_ORG` differs from the lock's org"))

        # skills: canonical in .agents/skills, linked for Claude
        link, canon = root / CLAUDE_SKILLS, root / SKILLS
        if canon.is_dir() and not canon.is_symlink():
            if not (link.is_symlink() and os.readlink(link) == SKILLS_LINK):
                out.append(finding("sync.skills", "error", CLAUDE_SKILLS, None, f"must be a symlink to {SKILLS_LINK}"))
        elif link.is_dir() and not link.is_symlink():
            out.append(finding("sync.skills", "error", CLAUDE_SKILLS, None, f"skills belong in {SKILLS}; run `estate-sync` to move them"))
    return out


# ---------------------------------------------------------------- write

def git_head(d):
    try:
        return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def vendor(root, lock, hooks, estate):
    estate = Path(estate)
    src_dir = estate / "hooks"
    ids = [h["id"] for h in hooks if not h["run"]]
    for hid in ids:
        src = src_dir / f"{hid}.sh"
        if not src.is_file():
            raise SpecError(f"estate hook {hid!r} not found at {src}")
        dst = root / VENDOR_DIR / f"{hid}.sh"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        dst.chmod(0o755)
        lock["vendored"][hid] = sha256(dst)
    for hid in set(lock["vendored"]) - set(ids):
        del lock["vendored"][hid]
        (root / VENDOR_DIR / f"{hid}.sh").unlink(missing_ok=True)
    head = git_head(estate)
    if head:
        lock["estate"] = {"repo": lock["estate"].get("repo", "rarebit-one/agent-estate"), "sha": head}


def sync(root, estate=None):
    """Write every rendered file. Returns the repo-relative paths it changed."""
    root = Path(root)
    lock, hooks = load_lock(root), load_hooks(root)
    changed = []

    def write(rel, text):
        p = root / rel
        if not p.exists() or p.read_text() != text:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            changed.append(rel)

    if estate:
        before = {rel: (root / rel).read_bytes() for rel in [f"{VENDOR_DIR}/{i}.sh" for i in lock["vendored"]] if (root / rel).exists()}
        vendor(root, lock, hooks, estate)
        for hid in lock["vendored"]:
            rel = f"{VENDOR_DIR}/{hid}.sh"
            if before.get(rel) != (root / rel).read_bytes():
                changed.append(rel)
        write(LOCK, dump_lock(lock))

    if "claude" in lock["targets"]:
        p = root / SETTINGS
        current = json.loads(p.read_text()) if p.exists() else {}
        want = render_settings(current, lock, hooks)
        if want != current or not p.exists():
            write(SETTINGS, dump_json(want))

        link, canon = root / CLAUDE_SKILLS, root / SKILLS
        if link.is_dir() and not link.is_symlink():
            if canon.exists():
                raise SpecError(f"both {CLAUDE_SKILLS} and {SKILLS} are directories; merge them by hand")
            canon.parent.mkdir(parents=True, exist_ok=True)
            link.rename(canon)
            changed.append(SKILLS)
        if canon.is_dir() and not (link.is_symlink() and os.readlink(link) == SKILLS_LINK):
            if link.is_symlink():
                link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(SKILLS_LINK)
            changed.append(CLAUDE_SKILLS)
    return changed


def init(root, estate):
    """Derive hooks.toml + estate.lock from the hooks registered in .claude/settings.json today."""
    root, estate = Path(root), Path(estate)
    if (root / LOCK).exists() or (root / HOOKS).exists():
        raise SpecError(f"{LOCK} or {HOOKS} already exists")
    estate_ids = {p.stem for p in (estate / "hooks").glob("*.sh")}
    settings = json.loads((root / SETTINGS).read_text()) if (root / SETTINGS).exists() else {}
    events_rev = {v: k for k, v in CLAUDE_EVENTS.items()}
    tools_rev = {v: k for k, v in CLAUDE_TOOLS.items()}
    hooks, by_id, superseded = [], {}, []
    for event, groups in (settings.get("hooks") or {}).items():
        if event not in events_rev:
            raise SpecError(f"{SETTINGS}: event {event!r} has no estate equivalent")
        on = events_rev[event]
        for g in groups:
            matcher = g.get("matcher") or ""
            tools = None
            if on in TOOL_EVENTS:
                names = matcher.split("|")
                if not matcher or any(n not in tools_rev for n in names):
                    raise SpecError(f"{SETTINGS}: {event} matcher {matcher!r} is not expressible as tool classes")
                tools = [tools_rev[n] for n in names]
            elif matcher:
                raise SpecError(f"{SETTINGS}: {event} has matcher {matcher!r}, which the estate schema has no field for")
            for e in g.get("hooks", []):
                cmd = e.get("command", "")
                if e.get("type") != "command" or not cmd.startswith(PROJECT_DIR) or " " in cmd.strip():
                    raise SpecError(f"{SETTINGS}: hook {cmd or e!r} is not a plain $CLAUDE_PROJECT_DIR script")
                rel = cmd[len(PROJECT_DIR):]
                stem = Path(rel).stem
                is_estate = stem in estate_ids and Path(rel).suffix == ".sh"
                hid = stem
                if hid not in by_id:
                    h = {"id": hid, "run": None if is_estate else rel, "timeout": e.get("timeout"), "events": []}
                    by_id[hid] = h
                    hooks.append(h)
                    if is_estate:
                        superseded.append(rel)
                ev = {"on": on} | ({"tools": tools} if tools else {})
                if ev not in by_id[hid]["events"]:
                    by_id[hid]["events"].append(ev)
    (root / ".agents").mkdir(exist_ok=True)
    (root / HOOKS).write_text(dump_hooks(hooks))
    (root / LOCK).write_text(dump_lock({"org": settings.get("env", {}).get("CLAUDE_WS_ORG"), "targets": ["claude"], "estate": {}, "vendored": {}}))
    for rel in superseded:  # the old per-repo copies; the vendored ones replace them
        (root / rel).unlink(missing_ok=True)
    changed = [HOOKS, LOCK] + superseded
    return changed + [c for c in sync(root, estate) if c not in changed]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--check", action="store_true", help="report drift and exit 1 on any; write nothing")
    ap.add_argument("--estate", help="agent-estate checkout to vendor estate hooks from")
    ap.add_argument("--init", action="store_true", help="create hooks.toml + estate.lock from .claude/settings.json (needs --estate)")
    a = ap.parse_args()
    try:
        if a.check:
            res = check(a.path)
            for f in res:
                print(f"{f['severity']:5} {f['rule']:14} {f['file']}  {f['message']}")
            if not (Path(a.path) / LOCK).exists():
                print(f"{a.path}: no {LOCK}; nothing to check")
            sys.exit(1 if any(f["severity"] == "error" for f in res) else 0)
        if a.init and not a.estate:
            ap.error("--init needs --estate")
        changed = init(a.path, a.estate) if a.init else sync(a.path, a.estate)
    except SpecError as e:
        print(f"estate-sync: {e}", file=sys.stderr)
        sys.exit(2)
    root = Path(a.path)
    print("\n".join(f"{'wrote' if (root / c).exists() or (root / c).is_symlink() else 'removed'} {c}" for c in changed) or "up to date")


if __name__ == "__main__":
    main()
