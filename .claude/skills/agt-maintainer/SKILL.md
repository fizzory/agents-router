---
name: agt-maintainer
description: How to maintain the Agents Router (agt) repository, including the macOS menu bar app under macos/ and its GitHub release. Covers changing its code, plugin files (agents, skills, commands, hooks), docs or tests; fixing a bug or adding a feature; running the tests; a live check in the sandbox; reviewing a change; and preparing a release or pull request. Use it whenever you work inside this repository.
---

# Maintaining Agents Router

This is the procedure. The facts it relies on, such as the layout, the rules for tests and the fake CLIs, are in [AGENTS.md](../../../AGENTS.md); read it first.

## 1. Start

1. Run `git status` and `git branch --show-current`. Never commit to `main`; branch first (`git switch -c claude/<topic>`).
2. Read `AGENTS.md`, then the files you'll change and their tests.
3. If this checkout holds another branch's uncommitted work, make the new fix in a worktree off `main` (`git worktree add ../<repo>-<topic> -b claude/<topic> main`), and tell every delegate the other checkout is off-limits. To bring a merged `main` under the waiting work later: save `git diff > <backup>.patch`, `git stash`, `git merge --ff-only origin/main`, `git stash pop`, then settle the version lines as §4 says.

## 2. Change

- Python 3.11+ and the standard library only. The one exception is `ui.py`, which loads rich inside functions for a person at a terminal. Agents, pipes, tests and hooks get its plain renderer.
- A new test module imports `tests/helpers.py`. Tests never call the real `claude`, `codex`, `agy` or `kiro-cli`, and never touch the real `~/.claude*`, `~/.codex`, `~/.gemini` or `~/.agents-router`. Teach the fake CLIs (`tests/fake_*.py`) what you need instead.
- A test never asserts on a fixed sleep or a fixed elapsed time. It waits, with a generous deadline, for the state it depends on: a file, a lock, a message or a status. Prove a timing fix by running the test repeatedly, idle and under CPU load. Slower CI runners expose every fixed wait.
- Changing any `agt … --json` output the app decodes is a contract change. Regenerate the producer fixtures (`tests/generate_usage_fixture.py`, `tests/generate_settings_fixtures.py`, `tests/generate_settings_inventory.py`). Decode real output, including in-flight, null and empty states, with `agt-app-check --stdin <kind>`. The whole-diff review covers both sides.
- Docs keep their style: short sentences, tables for lookups, no hedging, no "simply".

Which docs move with which change:

| Change | Also update |
|---|---|
| A CLI command or flag | README task table, `docs/*.md`, `src/agents_router/helptext.py` (the help page), relay agents in `agents/` if they run it |
| Setup, install, update, profiles | `src/agents_router/guides/setup.md`, then `python3 scripts/sync_guide.py` to copy it into `skills/agents-router-setup/SKILL.md` |
| Profiles or accounts | `docs/claude-code.md`, `docs/configuration.md`, the setup guide |
| Sharing | `docs/share.md` |
| Delegation behavior | `skills/agents-router/SKILL.md`, `commands/delegate.md`, `agents/agt-*.md` |
| Roles: the agents-router Roles section, setup's role step, `skills/orchestrator-*` | The cases in `tests/skill-evals/<skill>/` and its `triggers.json`; the skill gate in [references/release.md](references/release.md) |
| The macOS app (`macos/`) | `macos/README.md`, the source for its contracts and commands; don't copy them here. |

## 3. Test

```bash
export PYTHONPATH=src:tests
python3 -m unittest discover -s tests -t tests        # everything: over 10 minutes, longer than one shell call can hold
python3 -m unittest test_setup test_guide             # one or a few modules
```

- **The full run.** Start it in the background with its output in a log (`python3 -m unittest discover -s tests -t tests > "$TMPDIR/agt-suite.log" 2>&1`), then read only `grep -E '^(ERROR|FAIL):|^Ran |^OK|^FAILED' "$TMPDIR/agt-suite.log"`. Never pipe it through `tail -N`: that cuts the `ERROR:` header and the test's name. Any tester brief says the same.
- **Every failure blocks**, on every Python from 3.11 up, 3.15 included.
- **Unsure a failure is yours?** Run the same module on a clean tree with `git stash`, the test, then `git stash pop`.
- **A failure a rerun clears** is unexplained, not green. Before calling it flaky, rerunning or merging past it, answer: can the product hit this timing window? Presume a failure in code the change added is a product race until shown otherwise. Fix the product, then the test.
- **No test verdict while a release build runs.** Unchanged `client.rs` tests have failed and timed out during `build_app.sh`. Start the macOS tester after `build_app.sh` finishes. A red seen during a build is inconclusive: rerun it idle, and on `main`, before calling it flaky or blocking.
- **macOS app build names:** every build you make is `Agents Router Test.app`; only `scripts/build_app.sh` makes `Agents Router.app`. Locations and cleanup, data folders and login item included: `macos/README.md`, "Test and release builds". The exception: a hands-on check on the person's Mac uses the release build, installed over their copy with their OK, per [references/hands-on-check.md](references/hands-on-check.md).
- **macOS app:** the commands in `macos/README.md`. Every cargo call uses `CARGO_HOME=$PWD/build/cargo-home` (set from the repository root) and `--locked`. Build the UI first (`python3 scripts/build_app_ui.py`; the crate embeds `build/ui-dist`), then run `cargo test --locked` in `macos/src-tauri`, `node --test macos/ui/logic` and `python3 scripts/build_app_ui.py --test`. Installing a toolchain (Rust, or tauri-cli, Node and the UI's npm packages through `scripts/prepare_app_tools.sh`) needs the person's yes. To check a GUI fix by the person's click: [references/hands-on-check.md](references/hands-on-check.md).

## 4. A change the plugin ships

Anything the plugin ships counts: `agents/`, `skills/`, `commands/`, `hooks/`, the MCP server, or the `agt` code those call.

Raise `version` in these three files together:
- `.claude-plugin/plugin.json`
- `pyproject.toml`
- `src/agents_router/__init__.py`

These three are only part of the version set. Every bump moves everything `scripts/build_app.py` `versions()` checks, the app's files included; take the list from that function, not from a past release's diff. `tests/test_app_sources.py` runs it, and release.yml's step "Require exact release tag and matching package versions" asserts it against the tag. Check it from the repository root with `python3 -c 'import sys; sys.path.insert(0, "scripts"); from build_app import versions; versions()'`: it prints `Release version: X.Y.Z` or names the files that disagree. Then run `PYTHONPATH=src:tests python3 tests/generate_usage_fixture.py`: the app fixtures embed the version.

Release notes go in the PR body. There is no changelog file, and the GitHub release uses generated notes.

A **minor** bump also changes the setup guide's sentence "This guide is for agt X.Y.Z or newer" in `src/agents_router/guides/setup.md` (`tests/test_guide.py` checks it names the major.minor). Then run `python3 scripts/sync_guide.py`. That guide change selects the `agents-router-setup` eval test ([release.md](references/release.md) step 3), so plan for that test on every minor release.

Before bumping, check the version lines of the open PRs: `gh pr list --json number,title,headRefName`, then `gh pr diff <n> --exclude '*.lock' | grep -E '^\+(version|__version__) = '` for each (no output: that PR bumps nothing). Never take a version an open PR already owns. A side-task brief from a session that holds a release PR says so: "don't bump; PR #N owns X.Y.Z", or it names the next version.

When `main` changed the version under a branch that also bumps it, the version files conflict, and so do `src/agents_router/guides/setup.md` and `skills/agents-router-setup/SKILL.md`. Take `main`'s text, re-apply the branch's version to the whole `versions()` set, regenerate the fixtures and run `python3 scripts/sync_guide.py`, then the `versions()` check and `python3 -m unittest test_guide`.

Claude Code updates an installed plugin only when that version changes. `tests/test_guide.py` checks that the three match.

## 5. Live check: only in the sandbox, only with an OK

Real jobs use the person's quota. Before running one, ask, and say roughly what it uses: one Haiku job costs about a cent. Everything runs in the sandbox, which keeps the person's real setup untouched:

```bash
scripts/sandbox.sh create      # once; `reinstall` after each code change
claude-agt-a                   # Claude Code on sandbox profile a; claude-agt-b for profile b
agt-sandbox <command>          # one command inside the sandbox; alone, a sandbox shell
```

- The launchers work from any terminal and change nothing in it: the person's own `claude` and `agt` stay their real ones.
- The person logs in to `claude-agt-a` and `claude-agt-b` themselves. Their logins go to the sandbox's own keychain; the sandbox's `claude` runs only on its named profiles.
- Work through the scenarios in [references/live-check.md](references/live-check.md).
- Kiro is the exception: the sandbox logs it out, so it's checked with a scratch `AGT_HOME` on the real Kiro login ([references/live-check.md §7](references/live-check.md#7-kiro-outside-the-sandbox-with-a-scratch-agt-home)).
- Finish with `scripts/sandbox.sh destroy` when the person agrees.

## 6. Review

- **An independent pass.** Start `agents-router:agt-codex` with `access=read`. Give it the diff: `git diff main...HEAD` once the work is committed. Before that (the usual case, since you commit only when asked), run `git add -N <new files>` and give `git diff main`, which then includes the new files. Give it the plan too, never your conclusions, and ask for bugs and gaps.
- **Open choices.** Settle them with the decision-maker agents (`system-decision-maker`, `ux-decision-maker`, `general-decision-maker`) instead of asking the person, unless the choice is theirs: scope, money or anything outward-facing.
- **Fold the findings in.** Then tell the person what you left out, and why.

## 7. Release

Steps and exact commands: [references/release.md](references/release.md).

In short:
1. Run the full suite. When the change touches a skill with cases in `tests/skill-evals/`, that skill's graded cases must pass too, and only those (`--tag <skill>`, `-j 4`): the eval test, on the person's subscription quota: ask once, and never shape the work to avoid it. The benchmark and trigger tuning only advise.
2. Raise the version.
3. Commit with the subject `Agents Router X.Y.Z: <what changed for the person>`.
4. Push the branch and open a pull request. Then watch its CI: start `/mentor:merge`, or `gh pr checks <n> --watch` in the background. Triage a red job before the person has to report it.
5. After any merge, whoever merged (you, `/mentor:merge`, or the person), run `agt update --dry-run`, then `agt update`, and tell the person to `/reload-plugins`.
6. When the app changed: tag, check the draft, publish on the person's word ([release.md](references/release.md) step 8). A merge alone ships no installer.

Commit or push only when the person asks.

## 8. Never

| Never | Instead |
|---|---|
| Read, copy or print credential files, or values from `.claude.json` | Hashes and `claude auth status` only |
| Touch the real `~/.claude*`, `~/.codex` or `~/.agents-router` from a test | The fakes and `tests/helpers.py` |
| Run `agt doctor --live` or a real job outside the sandbox, or without an OK | The sandbox, after asking (Kiro: a scratch `AGT_HOME`, [live-check §7](references/live-check.md#7-kiro-outside-the-sandbox-with-a-scratch-agt-home)) |
| Edit `~/.agents-router/homes/` | agt rebuilds those folders |
| Reach a Claude profile with `env -u CLAUDE_CONFIG_DIR` or `agt run` in the shell | `agents-router:agt-claude` with `account=<profile>` |
