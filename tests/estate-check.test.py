#!/usr/bin/env python3
"""Fixture tests for estate-check.py: each rule fires on a bad repo, and a clean repo passes."""
import importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("estate_check", HERE.parent / "estate-check.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)


def repo(files):
    d = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "-C", str(d), "add", "-A"], check=True)  # ignore personal global ignores
    return d


def rules(d, **kw):
    return {(f["rule"], f["severity"]) for f in ec.check(d, **kw)}


CLEAN = {
    "AGENTS.md": "# repo\nRun `bin/test`.\n",
    "CLAUDE.md": "@AGENTS.md\n",
    "bin/test": "#!/bin/sh\n",
    ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(bin/test:*)", "Bash(git status:*)"]}}),
    ".github/workflows/review.yml": "jobs:\n  r:\n    steps:\n      - with:\n          allowed_bots: 'rarebit-one,claude'\n          claude_args: '--model ${{ vars.AGENT_MODEL }}'\n",
    ".mcp.json": json.dumps({"mcpServers": {"x": {"command": "npx", "args": ["-y", "@acme/mcp@1.2.3"]}}}),
}


class EstateCheck(unittest.TestCase):
    def test_clean_repo_has_no_findings(self):
        self.assertEqual(ec.check(repo(CLEAN)), [])

    def test_bypass_and_bare_bash_allows(self):
        d = repo({**CLEAN, ".claude/settings.json": json.dumps({"permissions": {"allow": [
            "Bash", "Bash(SKIP_SIGNED_COMMITS_HOOK=1 git commit:*)", "Bash(pkexec sh -c *)", "Bash(ssh:*)"]}})})
        r = rules(d)
        self.assertIn(("perms.bare-bash", "error"), r)
        self.assertIn(("perms.bypass", "error"), r)
        self.assertIn(("perms.broad", "warn"), r)

    def test_committed_local_settings(self):
        d = repo({**CLEAN, ".claude/settings.local.json": "{}"})
        self.assertIn(("perms.local-committed", "error"), rules(d))

    def test_ci_rules(self):
        wf = ("jobs:\n  a:\n    steps:\n      - with:\n          allowed_bots: '*'\n"
              "          allowed_tools: 'Bash,Read,Bash(gh pr view:*)'\n"
              "          claude_args: '--model ${{ vars.CLAUDE_MODEL || ''claude-sonnet-4-6'' }}'\n")
        r = rules(repo({**CLEAN, ".github/workflows/agent.yml": wf}))
        self.assertIn(("ci.allowed-bots", "error"), r)
        self.assertIn(("ci.bare-bash", "error"), r)
        self.assertIn(("ci.literal-model", "warn"), r)

    def test_scoped_bash_only_is_not_bare(self):
        wf = "jobs:\n  a:\n    steps:\n      - with:\n          allowed_tools: 'Read,Bash(gh pr view:*),Bash(node:*)'\n"
        self.assertNotIn(("ci.bare-bash", "error"), rules(repo({**CLEAN, ".github/workflows/agent.yml": wf})))

    def test_unpinned_mcp(self):
        d = repo({**CLEAN, ".mcp.json": json.dumps({"mcpServers": {
            "a": {"command": "npx", "args": ["-y", "@supabase/mcp-server-supabase@latest"]},
            "b": {"command": "npx", "args": ["@zilliz/claude-context-mcp"]}}})})
        self.assertEqual(sum(f["rule"] == "mcp.unpinned" for f in ec.check(d)), 2)

    def test_mac_path_and_dead_path(self):
        d = repo({**CLEAN, "AGENTS.md": "cd /Users/someone/Workspace/x\nSee `bin/missing.sh` and `bin/test`.\n"})
        r = rules(d)
        self.assertIn(("paths.mac", "error"), r)
        dead = [f for f in ec.check(d) if f["rule"] == "paths.dead"]
        self.assertEqual([f["message"].split("`")[1] for f in dead], ["bin/missing.sh"])

    def test_budgets(self):
        big = "line\n" * 250
        r = rules(repo({**CLEAN, "AGENTS.md": big}))
        self.assertIn(("budget.file", "error"), r)
        self.assertIn(("budget.file", "error"), rules(repo({**CLEAN, "AGENTS.md": "x\n" * 130}), workspace=True))
        self.assertIn(("budget.loaded", "error"), rules(repo({**CLEAN, "AGENTS.md": ("y" * 170 + "\n") * 150})))

    def test_agents_md_missing_is_info(self):
        d = repo({"CLAUDE.md": "# x\n"})
        self.assertIn(("portability.agents-md", "info"), rules(d))

    def test_hook_drift_only_with_hooks_dir(self):
        canon = Path(tempfile.mkdtemp())
        (canon / "guard.sh").write_text("#!/bin/sh\nexit 0\n")
        d = repo({**CLEAN, ".claude/hooks/guard.sh": "#!/bin/sh\nexit 1\n"})
        self.assertNotIn(("hooks.drift", "warn"), rules(d))
        self.assertIn(("hooks.drift", "warn"), rules(d, hooks_dir=canon))
        same = repo({**CLEAN, ".claude/hooks/guard.sh": "#!/bin/sh\nexit 0\n"})
        self.assertNotIn(("hooks.drift", "warn"), rules(same, hooks_dir=canon))


def cli(d, *args, env=None):
    return subprocess.run([sys.executable, str(HERE.parent / "estate-check.py"), *args, str(d)],
                          capture_output=True, text=True, cwd=d, env=env)


class Cli(unittest.TestCase):
    def test_exit_codes(self):
        bad = repo({**CLEAN, ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash"]}})})
        self.assertEqual(cli(bad).returncode, 1)
        self.assertEqual(cli(bad, "--report").returncode, 0)
        self.assertEqual(cli(repo(CLEAN)).returncode, 0)

    def test_github_format(self):
        bad = repo({**CLEAN, "AGENTS.md": "a|b /Users/someone/x\n", "CLAUDE.md": "# no import\n",
                    ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(ssh:*)"]}})})
        (bad / "AGENTS.md").unlink()
        subprocess.run(["git", "-C", str(bad), "rm", "-q", "--cached", "AGENTS.md"], check=True)
        (bad / "CLAUDE.md").write_text("a|b /Users/someone/x\n")
        summ = Path(tempfile.mkdtemp()) / "summary.md"
        r = cli(bad, "--format", "github", "--report", env={**os.environ, "GITHUB_STEP_SUMMARY": str(summ)})
        self.assertEqual(r.returncode, 0)
        self.assertIn("::error file=CLAUDE.md,line=1,title=paths.mac::", r.stdout)
        self.assertIn("::warning file=.claude/settings.json,title=perms.broad::", r.stdout)
        self.assertIn("::notice file=CLAUDE.md,title=portability.agents-md::", r.stdout)
        text = summ.read_text()
        self.assertIn("| error | `paths.mac` | `CLAUDE.md:1` |", text)
        self.assertIn("1 error, 1 warn, 1 info", text)

    def test_json_alias(self):
        r = cli(repo(CLEAN), "--json")
        self.assertEqual(list(json.loads(r.stdout).values()), [[]])


if __name__ == "__main__":
    unittest.main(verbosity=1)
