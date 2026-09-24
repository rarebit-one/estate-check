#!/usr/bin/env python3
"""
estate-check — lint a repo (or an org workspace root) for the agent-config rules
the estate agreed on. Read-only; stdlib only, so it runs anywhere.

  estate-check.py [PATH ...]                   # default: current directory
  estate-check.py --workspace ~/Workspace/<org>   # an org workspace root (not a git repo)
  estate-check.py --format json PATH           # machine-readable findings (--json is an alias)
  estate-check.py --format github PATH         # GitHub Actions annotations + job summary
  estate-check.py --report PATH                # always exit 0 (baseline / report-only)
  estate-check.py --hooks-dir DIR PATH         # also compare committed hook copies with DIR

Exit 1 when any `error` finding exists (unless --report). Rules, by id:

  budget.file          an instruction file is over its line budget
  budget.loaded        the always-loaded instructions are over the token budget
  paths.mac            a macOS home path (/Users/<name>/…) in agent config
  paths.dead           a backticked repo path in an instruction/skill file that doesn't exist
  perms.bypass         an allow rule pre-approves a signing/hook bypass or root
  perms.bare-bash      an allow list contains a bare `Bash` (makes scoped rules moot)
  perms.broad          an allow rule that grants a whole CLI (warn)
  perms.local-committed  .claude/settings.local.json is tracked
  ci.allowed-bots      allowed_bots: "*" on an agent step
  ci.bare-bash         an agent step's tool allowlist contains a bare `Bash`
  ci.literal-model     a literal model id in a workflow (use an org variable)
  mcp.unpinned         an MCP server launched from an unpinned (@latest / bare) package
  hooks.drift          a committed hook copy differs from the canonical copy (needs --hooks-dir)
  hooks.dangling       a hook symlink resolves to nothing (workspace mode)
  portability.agents-md  CLAUDE.md without AGENTS.md (info)
  skills.model-pin     a SKILL.md pins a vendor model in frontmatter (info)

Budgets follow the claude-md-shape convention: a repo instruction file ≤120 lines
(warn) / ≤200 (error); an org workspace root ≤60 / ≤120; always-loaded text
≤3k tokens (warn) / ≤6k (error). Tokens are estimated as characters / 4.
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

INSTRUCTION_NAMES = {"CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", "GEMINI.md", ".windsurfrules", ".cursorrules"}
AGENT_CONFIG_PREFIXES = (".claude/", ".agents/", ".codex/", ".cursor/", ".github/")
MAC_PATH = re.compile(r"/Users/[a-z][\w.-]*/")
MODEL_ID = re.compile(r"""(?<![\w/.-])(claude-(?:opus|sonnet|haiku|fable|mythos)-\d[\w.-]*|gpt-\d[\w.-]*|o\d-(?:mini|pro)\b)""")
BYPASS = re.compile(r"SKIP_SIGNED_COMMITS_HOOK|SKIP_PRE_PUSH_PREPARE|--no-gpg-sign|commit\.gpgsign=false|\bpkexec\b|\bsudo\b|\bop (?:item|read)\b|--no-verify\b")
BROAD = re.compile(r"^Bash\((?:ssh|scp|curl|wget|python3?|node|gh api|gh|git|bundle|npm|npx|doctl|docker|kubectl|systemctl|rm|cmd /c|powershell[^)]*)(?::\*| \*)\)$")
BACKTICK_PATH = re.compile(r"`((?:\.[\w-]+|[\w-]+)/[\w./-]*[\w-])`")


def finding(rule, sev, path, line, msg):
    return {"rule": rule, "severity": sev, "file": str(path), "line": line, "message": msg}


def tracked_files(root):
    """Every path git tracks (including sparse-excluded ones), else a filesystem walk."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        files = [p for p in out.decode().split("\0") if p]
        if files:
            return files, True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Not a repo (an org workspace root): walk it, but never descend into a
    # nested repo or worktree — those are checked on their own.
    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns
                  if d not in {".git", "node_modules", ".worktrees", "worktrees"}
                  and not d.endswith(".pre-wsc.bak")
                  and not os.path.exists(os.path.join(dp, d, ".git"))]
        files += [os.path.relpath(os.path.join(dp, f), root) for f in fns]
        # os.walk lists a directory symlink as a dir and never enters it; record it
        # as an entry, like git ls-files does, so path lookups can resolve through it.
        files += [os.path.relpath(os.path.join(dp, d), root) for d in dns if os.path.islink(os.path.join(dp, d))]
    return files, False


def read(root, rel):
    p = root / rel
    if p.is_symlink() and not p.exists():
        return None
    try:
        return p.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


def is_instruction(rel):
    name = rel.rsplit("/", 1)[-1]
    return name in INSTRUCTION_NAMES or (rel.startswith(".claude/rules/") and rel.endswith(".md"))


def check(root, workspace=False, links=None, hooks_dir=None):
    """links: resolve hook symlinks (default: only for a real workspace root on disk).
    hooks_dir: canonical hooks to compare committed copies against (hooks.drift); None skips it."""
    root = Path(root).resolve()
    files, is_git = tracked_files(root)
    fileset = set(files)
    dirs = {"/".join(f.split("/")[:k]) for f in files for k in range(1, f.count("/") + 1)}
    tops = {f.split("/", 1)[0] for f in files}
    # A tracked symlink to a directory inside the repo (e.g. .claude/skills ->
    # ../.agents/skills) makes every file under its target reachable at the link
    # path too, transitively through nested links. Only path lookups see these
    # aliases; no file is linted twice.
    links = []
    for rel in files:
        p = root / rel
        if not (p.is_symlink() and p.is_dir()):
            continue
        try:
            target = p.resolve().relative_to(root).as_posix()
        except ValueError:
            continue  # points outside the repo
        links.append((rel, "" if target == "." else target + "/"))
    for _ in range(4):  # bounded: a link to an ancestor would otherwise nest forever
        new = {rel + "/" + f[len(t):] for rel, t in links for f in fileset if f.startswith(t) and f != rel} - fileset
        if not new:
            break
        fileset |= new
        for alias in new:
            dirs.update("/".join(alias.split("/")[:k]) for k in range(1, alias.count("/") + 1))
    dirs.update(rel for rel, _ in links)
    out = []

    # --- budgets -------------------------------------------------------------
    warn_lines, err_lines = (60, 120) if workspace else (120, 200)
    loaded_chars = 0
    for rel in files:
        if not is_instruction(rel):
            continue
        text = read(root, rel)
        if text is None:
            continue
        n = text.count("\n") + (0 if text.endswith("\n") else 1)
        if rel in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md") or rel.startswith(".claude/rules/"):
            loaded_chars += len(text)
        elif rel == "AGENTS.md" and "CLAUDE.md" not in fileset:
            loaded_chars += len(text)
        # an @AGENTS.md import makes AGENTS.md always loaded too
        if rel == "CLAUDE.md" and re.search(r"^@AGENTS\.md\s*$", text, re.M) and "AGENTS.md" in fileset:
            loaded_chars += len(read(root, "AGENTS.md") or "")
        if n > err_lines:
            out.append(finding("budget.file", "error", rel, None, f"{n} lines (budget {warn_lines}, hard cap {err_lines})"))
        elif n > warn_lines:
            out.append(finding("budget.file", "warn", rel, None, f"{n} lines (budget {warn_lines})"))
    tok = loaded_chars // 4
    if tok > 6000:
        out.append(finding("budget.loaded", "error", ".", None, f"~{tok} tokens always loaded (budget 3000, hard cap 6000)"))
    elif tok > 3000:
        out.append(finding("budget.loaded", "warn", ".", None, f"~{tok} tokens always loaded (budget 3000)"))

    if "CLAUDE.md" in fileset and "AGENTS.md" not in fileset:
        out.append(finding("portability.agents-md", "info", "CLAUDE.md", None, "no AGENTS.md: other vendors' agents see no instructions"))

    # --- per-file text rules -------------------------------------------------
    for rel in files:
        agent_cfg = is_instruction(rel) or rel.startswith(AGENT_CONFIG_PREFIXES) or rel == ".mcp.json"
        if not agent_cfg:
            continue
        text = read(root, rel)
        if text is None:
            continue
        lines = text.splitlines()

        for i, ln in enumerate(lines, 1):
            if MAC_PATH.search(ln):
                out.append(finding("paths.mac", "error", rel, i, "macOS home path: use $HOME / Path.home()"))

        if is_instruction(rel) or rel.endswith("SKILL.md"):
            base = rel.rsplit("/", 1)[0] if "/" in rel else ""
            for i, ln in enumerate(lines, 1):
                for m in BACKTICK_PATH.finditer(ln):
                    p = m.group(1).rstrip("/.")
                    first = p.split("/", 1)[0]
                    if first not in tops:  # only paths that claim to be inside this repo
                        continue
                    cands = [p, f"{base}/{p}" if base else p]
                    if not any(c in fileset or c in dirs for c in cands):
                        out.append(finding("paths.dead", "warn", rel, i, f"`{p}` does not exist in this repo"))

        if rel.endswith("SKILL.md"):
            fm = re.match(r"---\n(.*?)\n---", text, re.S)
            if fm and re.search(r"^model:\s*\S", fm.group(1), re.M):
                out.append(finding("skills.model-pin", "info", rel, None, "pins a vendor model in frontmatter (move to metadata)"))

        if rel.startswith(".claude/") and rel.endswith(".json") and "settings" in rel:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                out.append(finding("perms.bypass", "warn", rel, None, "settings file is not valid JSON"))
                continue
            allow = ((data.get("permissions") or {}).get("allow")) or []
            for rule in allow:
                if not isinstance(rule, str):
                    continue
                if rule in ("Bash", "Bash(*)", "Bash(:*)"):
                    out.append(finding("perms.bare-bash", "error", rel, None, f"allow rule {rule!r} allows every command"))
                elif BYPASS.search(rule):
                    out.append(finding("perms.bypass", "error", rel, None, f"allow rule pre-approves a bypass: {rule[:90]}"))
                elif BROAD.match(rule):
                    out.append(finding("perms.broad", "warn", rel, None, f"broad allow rule: {rule}"))
            if rel.endswith("settings.local.json") and is_git:
                out.append(finding("perms.local-committed", "error", rel, None, "personal settings are committed; gitignore it"))

        if rel.startswith(".github/") and rel.endswith((".yml", ".yaml")):
            for i, ln in enumerate(lines, 1):
                s = ln.strip()
                if s.startswith("#"):
                    continue
                if re.search(r"""allowed_bots:\s*['"]?\*['"]?\s*$""", s):
                    out.append(finding("ci.allowed-bots", "error", rel, i, 'allowed_bots: "*" lets any bot trigger the agent'))
                m = re.search(r"""(allowed[_-]tools|allowedTools)\W+(.*)$""", s)
                if m and re.search(r"""(^|['",\s])Bash(\s*[,'"]|\s*$)""", m.group(2)):
                    out.append(finding("ci.bare-bash", "error", rel, i, "tool allowlist contains a bare Bash"))
                for mm in MODEL_ID.finditer(s):
                    out.append(finding("ci.literal-model", "warn", rel, i, f"literal model id {mm.group(1)}: read it from an org variable"))

        if rel == ".mcp.json" or rel.endswith("/.mcp.json"):
            try:
                servers = (json.loads(text).get("mcpServers") or {})
            except json.JSONDecodeError:
                servers = {}
            for name, spec in servers.items():
                args = [a for a in (spec.get("args") or []) if isinstance(a, str)]
                if spec.get("command") in ("npx", "bunx", "uvx", "pnpx") or any(a.startswith("@") for a in args):
                    pkgs = [a for a in args if not a.startswith("-") and ("@" in a or "/" in a)]
                    for p in pkgs[:1]:
                        ver = p.rsplit("@", 1)[1] if p.count("@") >= (2 if p.startswith("@") else 1) else ""
                        if ver in ("", "latest", "next"):
                            out.append(finding("mcp.unpinned", "error", rel, None, f"MCP server {name!r} runs unpinned {p}"))

    # --- hooks ---------------------------------------------------------------
    for rel in files:
        if not rel.startswith(".claude/hooks/") or rel.count("/") != 2:
            continue
        p = root / rel
        canon = Path(hooks_dir) / p.name if hooks_dir else None
        if p.is_symlink():
            if (workspace if links is None else links) and not p.exists():
                out.append(finding("hooks.dangling", "error", rel, None, f"symlink to {os.readlink(p)} resolves to nothing"))
            continue
        if canon and canon.is_file() and p.is_file() and p.read_bytes() != canon.read_bytes():
            out.append(finding("hooks.drift", "warn", rel, None, f"differs from the canonical {canon}"))
    return out


SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


def render(root, results):
    by = {}
    for f in results:
        by.setdefault(f["severity"], []).append(f)
    head = f"{root}: " + ", ".join(f"{len(by.get(s, []))} {s}" for s in ("error", "warn", "info"))
    lines = [head]
    for f in sorted(results, key=lambda f: (SEV_ORDER[f["severity"]], f["rule"], f["file"], f["line"] or 0)):
        loc = f["file"] + (f":{f['line']}" if f["line"] else "")
        lines.append(f"  {f['severity']:5} {f['rule']:22} {loc}  {f['message']}")
    return "\n".join(lines)


ANNOTATION = {"error": "error", "warn": "warning", "info": "notice"}


def _esc(v, prop=False):
    v = str(v).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return v.replace(":", "%3A").replace(",", "%2C") if prop else v


def github(root, results):
    """Workflow-command annotations (paths relative to the job's working directory)."""
    lines = []
    base = Path(root).resolve()
    for f in sorted(results, key=lambda f: (SEV_ORDER[f["severity"]], f["rule"], f["file"], f["line"] or 0)):
        rel = os.path.relpath(base / f["file"], Path.cwd())
        props = f"file={_esc(rel, True)}" + (f",line={f['line']}" if f["line"] else "") + f",title={_esc(f['rule'], True)}"
        lines.append(f"::{ANNOTATION[f['severity']]} {props}::{_esc(f['message'])}")
    return "\n".join(lines)


def summary(all_results):
    """Markdown for $GITHUB_STEP_SUMMARY: counts per path, then every finding."""
    out = ["## estate-check", ""]
    for root, res in all_results.items():
        n = {s: sum(f["severity"] == s for f in res) for s in ("error", "warn", "info")}
        out.append(f"**{root}**: {n['error']} error, {n['warn']} warn, {n['info']} info")
        out.append("")
        if res:
            out += ["| Severity | Rule | Location | Finding |", "|---|---|---|---|"]
            for f in sorted(res, key=lambda f: (SEV_ORDER[f["severity"]], f["rule"], f["file"], f["line"] or 0)):
                loc = f["file"] + (f":{f['line']}" if f["line"] else "")
                msg = f["message"].replace("|", "\\|")
                out.append(f"| {f['severity']} | `{f['rule']}` | `{loc}` | {msg} |")
            out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--workspace", action="store_true", help="treat PATH as an org workspace root (tighter budgets, hook symlinks)")
    ap.add_argument("--format", choices=("text", "json", "github"), default="text")
    ap.add_argument("--json", action="store_true", help="alias for --format json")
    ap.add_argument("--report", action="store_true", help="report only: exit 0 even with errors")
    ap.add_argument("--hooks-dir", help="canonical hooks directory; enables hooks.drift")
    a = ap.parse_args()
    fmt = "json" if a.json else a.format
    all_results, errors = {}, 0
    for path in a.paths:
        res = check(path, workspace=a.workspace, hooks_dir=a.hooks_dir)
        all_results[path] = res
        errors += sum(1 for f in res if f["severity"] == "error")
        if fmt in ("text", "github"):
            print(render(path, res))
        if fmt == "github" and res:
            print(github(path, res))
    if fmt == "json":
        json.dump({str(Path(k).resolve()): v for k, v in all_results.items()}, sys.stdout, indent=1)
        print()
    if fmt == "github" and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary(all_results))
    sys.exit(0 if a.report or errors == 0 else 1)


if __name__ == "__main__":
    main()
