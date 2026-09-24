# estate-check

Lint a repo's AI-agent configuration: `CLAUDE.md` / `AGENTS.md` size budgets,
permission rules that pre-approve bypasses, agent steps in CI that allow any bot
or any shell command, unpinned MCP servers, and paths that only exist on one
laptop.

It is one stdlib-only Python file. It only reads, never writes, and needs
nothing installed beyond `python3`, which every GitHub-hosted runner has.

## Use it in a workflow

```yaml
name: estate-check
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  estate-check:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: rarebit-one/estate-check@<sha> # v1.0.0
        with:
          mode: report   # or: enforce
```

| Input | Default | |
|---|---|---|
| `mode` | `report` | `report` annotates and never fails. `enforce` fails on any `error` finding. |
| `path` | `.` | The directory to check. It must be a git checkout, because findings are limited to tracked files. |
| `workspace` | `false` | `true` applies the tighter budgets for an org workspace root. |

The action writes each finding as an annotation on the offending line, and puts
the full table in the job summary.

Start a repo in `report` mode. Switch it to `enforce`, and make the check
required, once it comes up clean.

## Run it locally

```bash
./estate-check.py [PATH ...]              # exit 1 on any error finding
./estate-check.py --report PATH           # always exit 0
./estate-check.py --format json PATH      # machine-readable
./estate-check.py --workspace DIR         # an org workspace root (need not be a git repo)
./estate-check.py --hooks-dir DIR PATH    # also flag committed hook copies that differ from DIR
```

## Rules

| Rule | Severity | Catches |
|------|----------|---------|
| `budget.file` | warn / error | an instruction file over 120 / 200 lines (60 / 120 at a workspace root) |
| `budget.loaded` | warn / error | more than ~3k / 6k tokens of always-loaded instructions |
| `paths.mac` | error | a macOS home path (`/Users/<name>/…`) in agent config |
| `paths.dead` | warn | a backticked repo path in an instruction or skill file that doesn't exist |
| `perms.bypass` | error | an allow rule pre-approving `SKIP_*`, `--no-gpg-sign`, `--no-verify`, `sudo`, `pkexec`, `op item/read` |
| `perms.bare-bash` | error | a bare `Bash` allow rule |
| `perms.broad` | warn | an allow rule granting a whole CLI (`ssh`, `curl`, `gh api`, `python3`, …) |
| `perms.local-committed` | error | a tracked `.claude/settings.local.json` |
| `ci.allowed-bots` | error | `allowed_bots: "*"` on an agent step |
| `ci.bare-bash` | error | a bare `Bash` in an agent step's tool allowlist |
| `ci.literal-model` | warn | a literal model id in a workflow instead of a variable |
| `mcp.unpinned` | error | an MCP server run from an unpinned or `@latest` package |
| `hooks.drift` | warn | a committed hook copy that differs from `--hooks-dir` (off unless given) |
| `hooks.dangling` | error | a workspace hook symlink that resolves to nothing |
| `portability.agents-md` | info | a `CLAUDE.md` with no `AGENTS.md` |
| `skills.model-pin` | info | a `SKILL.md` pinning a vendor model in frontmatter |

Budgets are line counts, and tokens are estimated as characters ÷ 4. Short,
router-style instruction files are followed more reliably than long ones.
Procedures belong in skills, and rules belong in hooks or CI.

## Develop

```bash
python3 tests/estate-check.test.py
```

CI runs the tests, then runs the action against this repo in `enforce` mode. As
a negative control, it also runs the action against a deliberately bad fixture
and asserts that run fails.

## Release

Tags follow semver. Callers pin a full commit SHA with the version in a comment.

```bash
git tag -s v1.0.0 -m v1.0.0 && git push origin v1.0.0
git tag -fs v1 -m v1 v1.0.0 && git push -f origin v1   # moving major tag
```
