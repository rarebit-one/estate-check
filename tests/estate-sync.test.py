#!/usr/bin/env python3
"""Fixture tests for estate-sync.py: init → sync → check is clean, and every drift is caught."""
import importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("estate_sync", HERE.parent / "estate-sync.py")
es = importlib.util.module_from_spec(spec)
spec.loader.exec_module(es)
spec = importlib.util.spec_from_file_location("estate_check", HERE.parent / "estate-check.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)


def tree(files):
    d = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    return d


def estate():
    d = tree({"hooks/guard.sh": "#!/bin/sh\necho guard v1\n", "hooks/sign.sh": "#!/bin/sh\necho sign v1\n"})
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


SETTINGS = {
    "permissions": {"allow": ["Bash(bin/test:*)"]},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Edit|Write|NotebookEdit", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard.sh", "timeout": 10}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/sign.sh", "timeout": 30}]},
        ],
        "SessionStart": [{"hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/brief.sh"}]}],
    },
}


def legacy_repo():
    return tree({
        ".claude/settings.json": json.dumps(SETTINGS, indent=2) + "\n",
        ".claude/hooks/guard.sh": "#!/bin/sh\necho old guard\n",
        ".claude/hooks/sign.sh": "#!/bin/sh\necho sign v1\n",
        ".claude/hooks/brief.sh": "#!/bin/sh\necho brief\n",
        ".claude/skills/start/SKILL.md": "---\nname: start\ndescription: start work\n---\n",
    })


def rules(d):
    return {f["rule"] for f in es.check(d)}


class Init(unittest.TestCase):
    def setUp(self):
        self.est, self.repo = estate(), legacy_repo()
        es.init(self.repo, self.est)

    def test_init_then_check_is_clean(self):
        self.assertEqual(es.check(self.repo), [])

    def test_behaviour_is_preserved(self):
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(s["permissions"], SETTINGS["permissions"])  # unowned keys untouched
        pre = s["hooks"]["PreToolUse"]
        self.assertEqual([g["matcher"] for g in pre], ["Edit|Write|NotebookEdit", "Bash"])
        self.assertEqual(pre[0]["hooks"][0], {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.agents/hooks/guard.sh", "timeout": 10})
        self.assertEqual(s["hooks"]["SessionStart"][0]["hooks"][0]["command"], "$CLAUDE_PROJECT_DIR/.claude/hooks/brief.sh")
        self.assertNotIn("matcher", s["hooks"]["SessionStart"][0])

    def test_estate_hooks_vendored_and_old_copies_removed(self):
        self.assertEqual((self.repo / ".agents/hooks/guard.sh").read_text(), "#!/bin/sh\necho guard v1\n")
        self.assertFalse((self.repo / ".claude/hooks/guard.sh").exists())
        self.assertTrue((self.repo / ".claude/hooks/brief.sh").exists())  # repo-local hook stays
        self.assertTrue(os.access(self.repo / ".agents/hooks/sign.sh", os.X_OK))

    def test_skills_moved_and_linked(self):
        self.assertTrue((self.repo / ".agents/skills/start/SKILL.md").is_file())
        self.assertEqual(os.readlink(self.repo / ".claude/skills"), "../.agents/skills")

    def test_wrong_skills_link_is_caught_and_fixed(self):
        link = self.repo / ".claude/skills"
        link.unlink()
        link.symlink_to("/elsewhere/skills")
        self.assertEqual(rules(self.repo), {"sync.skills"})
        self.assertIn(".claude/skills", es.sync(self.repo))
        self.assertEqual(os.readlink(link), "../.agents/skills")

    def test_sync_is_idempotent(self):
        self.assertEqual(es.sync(self.repo, self.est), [])
        self.assertEqual(es.sync(self.repo), [])

    def test_hand_edited_hooks_key_is_drift(self):
        p = self.repo / ".claude/settings.json"
        s = json.loads(p.read_text())
        s["hooks"]["PreToolUse"].pop()
        p.write_text(json.dumps(s))
        self.assertEqual(rules(self.repo), {"sync.drift"})
        es.sync(self.repo)
        self.assertEqual(es.check(self.repo), [])

    def test_unowned_edits_and_reformatting_are_not_drift(self):
        p = self.repo / ".claude/settings.json"
        s = json.loads(p.read_text())
        s["permissions"]["allow"].append("Bash(npm test:*)")
        p.write_text(json.dumps(s))  # also drops the indentation
        self.assertEqual(es.check(self.repo), [])

    def test_edited_vendored_hook_is_caught(self):
        (self.repo / ".agents/hooks/sign.sh").write_text("#!/bin/sh\nexit 0\n")
        self.assertEqual(rules(self.repo), {"sync.vendored"})

    def test_new_estate_release_updates_lock(self):
        (self.est / "hooks/sign.sh").write_text("#!/bin/sh\necho sign v2\n")
        changed = es.sync(self.repo, self.est)
        self.assertIn(".agents/hooks/sign.sh", changed)
        self.assertIn(".agents/estate.lock", changed)
        self.assertEqual(es.check(self.repo), [])

    def test_hooks_toml_change_renders(self):
        p = self.repo / ".agents/hooks.toml"
        p.write_text(p.read_text() + '\n[[hook]]\nid = "late"\nrun = ".claude/hooks/brief.sh"\nevents = [{ on = "stop" }]\n')
        self.assertEqual(rules(self.repo), {"sync.drift"})
        es.sync(self.repo)
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertIn("Stop", s["hooks"])
        self.assertEqual(es.check(self.repo), [])

    def test_removed_hook_leaves_stale_vendored_entry(self):
        p = self.repo / ".agents/hooks.toml"
        p.write_text(p.read_text().replace('[[hook]]\nid = "sign"', '[[hook]]\nid = "sign-gone"\nrun = ".claude/hooks/brief.sh"'))
        self.assertIn("sync.vendored", rules(self.repo))
        es.sync(self.repo, self.est)
        self.assertFalse((self.repo / ".agents/hooks/sign.sh").exists())
        self.assertEqual(es.check(self.repo), [])

    def test_estate_check_reports_sync_rules(self):
        (self.repo / ".agents/hooks/sign.sh").write_text("tampered\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "-C", str(self.repo), "add", "-A"], check=True)
        found = {(f["rule"], f["severity"]) for f in ec.check(self.repo)}
        self.assertIn(("sync.vendored", "error"), found)


class Specs(unittest.TestCase):
    def lock(self, hooks_toml, lock='version = 1\ntargets = ["claude"]\n'):
        return tree({".agents/estate.lock": lock, ".agents/hooks.toml": hooks_toml})

    def test_no_lock_means_not_opted_in(self):
        self.assertEqual(es.check(tree({".claude/skills/x/SKILL.md": "x"})), [])

    def test_org_renders_env(self):
        d = self.lock("version = 1\n", 'version = 1\norg = "acme"\ntargets = ["claude"]\n')
        es.sync(d)
        self.assertEqual(json.loads((d / ".claude/settings.json").read_text()), {"env": {"CLAUDE_WS_ORG": "acme"}})
        self.assertEqual(es.check(d), [])

    def test_unverified_target_is_refused(self):
        d = self.lock("version = 1\n", 'version = 1\ntargets = ["claude", "codex"]\n')
        self.assertEqual(rules(d), {"sync.spec"})

    def test_bad_specs(self):
        for body in (
            'version = 1\n[[hook]]\nid = "x"\nrun = "a.sh"\nevents = [{ on = "pre_tool" }]\n',               # tools missing
            'version = 1\n[[hook]]\nid = "x"\nrun = "a.sh"\nevents = [{ on = "pre_tool", tools = ["grep"] }]\n',  # unknown class
            'version = 1\n[[hook]]\nid = "x"\nrun = "a.sh"\nevents = [{ on = "session_start", tools = ["bash"] }]\n',
            'version = 1\n[[hook]]\nid = "x"\nrun = "../a.sh"\nevents = [{ on = "stop" }]\n',
            'version = 1\n[[hook]]\nid = "x"\nrun = "a.sh"\nevents = [{ on = "on_boot" }]\n',
            'version = 1\n[[hook]]\nid = "x"\nrun = "a.sh"\nevents = [{ on = "stop" }]\n[[hook]]\nid = "x"\nrun = "a.sh"\nevents = [{ on = "stop" }]\n',
            'version = 2\n',
            'not toml = = \n',
        ):
            with self.subTest(body=body):
                self.assertEqual(rules(self.lock(body)), {"sync.spec"})

    def test_missing_run_path(self):
        d = self.lock('version = 1\n[[hook]]\nid = "x"\nrun = "nope.sh"\nevents = [{ on = "stop" }]\n')
        es.sync(d)
        self.assertEqual(rules(d), {"sync.spec"})

    def test_init_refuses_inexpressible_hooks(self):
        for hooks in (
            {"PreToolUse": [{"matcher": "mcp__x", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/a.sh"}]}]},
            {"Stop": [{"hooks": [{"type": "prompt", "prompt": "verify"}]}]},
            {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/a.sh"}]}]},
            {"Stop": [{"hooks": [{"type": "command", "command": "bash $CLAUDE_PROJECT_DIR/a.sh"}]}]},
        ):
            with self.subTest(hooks=hooks):
                d = tree({".claude/settings.json": json.dumps({"hooks": hooks})})
                with self.assertRaises(es.SpecError):
                    es.init(d, estate())
                self.assertFalse((d / ".agents/estate.lock").exists())

    def test_both_skill_dirs_is_an_error(self):
        d = tree({".agents/estate.lock": 'version = 1\n', ".agents/skills/a/SKILL.md": "a", ".claude/skills/b/SKILL.md": "b"})
        with self.assertRaises(es.SpecError):
            es.sync(d)


class VendoredFiles(unittest.TestCase):
    """[[file]] entries: a file copied byte-for-byte from another repo and hash-locked."""

    LOCK = (
        'version = 1\ntargets = []\n\n'
        '[[file]]\npath = ".github/workflows/deploy-production.yml"\nsource = "rarebit-one/.github"\n\n'
        '[[file]]\npath = ".agents/skills/release/SKILL.md"\nsource = "rarebit-one/.github"\nfrom = "skills/release/SKILL.md"\n'
    )

    def setUp(self):
        self.src = tree({".github/workflows/deploy-production.yml": "name: deploy v1\n",
                         "skills/release/SKILL.md": "---\nname: release\n---\n"})
        subprocess.run(["git", "init", "-q", str(self.src)], check=True)
        subprocess.run(["git", "-C", str(self.src), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        self.repo = tree({".agents/estate.lock": self.LOCK})

    def vendor(self):
        return es.sync(self.repo, sources={"rarebit-one/.github": str(self.src)})

    def test_vendor_then_check_is_clean(self):
        changed = self.vendor()
        self.assertIn(".github/workflows/deploy-production.yml", changed)
        self.assertEqual((self.repo / ".agents/skills/release/SKILL.md").read_text(), "---\nname: release\n---\n")
        lock = es.load_lock(self.repo)
        self.assertTrue(all(f["sha256"].startswith("sha256:") for f in lock["files"]))
        self.assertRegex(lock["sources"]["rarebit-one/.github"], r"^[0-9a-f]{40}$")
        self.assertEqual(es.check(self.repo), [])
        self.assertFalse((self.repo / ".claude/settings.json").exists(), "targets = [] renders nothing for Claude")
        self.assertEqual(self.vendor(), [], "a second run is a no-op")

    def test_hand_edit_is_caught(self):
        self.vendor()
        (self.repo / ".github/workflows/deploy-production.yml").write_text("name: deploy, edited\n")
        msgs = [f["message"] for f in es.check(self.repo) if f["rule"] == "sync.vendored"]
        self.assertTrue(any("rarebit-one/.github:.github/workflows/deploy-production.yml" in m for m in msgs), msgs)

    def test_missing_file_and_missing_hash_are_caught(self):
        self.assertEqual({f["rule"] for f in es.check(self.repo)}, {"sync.vendored"})  # never vendored: no hashes
        self.vendor()
        (self.repo / ".agents/skills/release/SKILL.md").unlink()
        self.assertIn("sync.vendored", rules(self.repo))

    def test_source_update_flows_through(self):
        self.vendor()
        (self.src / ".github/workflows/deploy-production.yml").write_text("name: deploy v2\n")
        self.assertEqual(self.vendor(), [".github/workflows/deploy-production.yml", ".agents/estate.lock"])
        self.assertEqual(es.check(self.repo), [])

    def test_executable_bit_follows_the_source_both_ways(self):
        wf = ".github/workflows/deploy-production.yml"
        (self.src / wf).chmod(0o755)
        self.vendor()
        self.assertTrue(os.access(self.repo / wf, os.X_OK))
        (self.src / wf).chmod(0o644)
        self.assertEqual(self.vendor(), [wf], "a mode-only change is reported")
        self.assertFalse(os.access(self.repo / wf, os.X_OK))

    def test_unknown_source_and_bad_paths_are_spec_errors(self):
        with self.assertRaises(es.SpecError):
            es.sync(self.repo, sources={"someone/else": str(self.src)})
        (self.repo / ".agents/estate.lock").write_text('version = 1\ntargets = []\n[[file]]\npath = "../x"\nsource = "a"\n')
        self.assertEqual(rules(self.repo), {"sync.spec"})


if __name__ == "__main__":
    unittest.main()
