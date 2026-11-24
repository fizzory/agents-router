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

- **The full run.** Start it in the background with its output in a log (`python3 -m unittest discover -s tests -t tests > "$TMPDIR/agt-suite.log" 2>&1`), then read only `grep -E '^(ERROR|FAIL):|^Ran |^OK|^FAILED' "$TMPDIR/agt-suite.log"`. Never pipe it through `tail -N`: that cuts the `ERROR:` header and the test's name. Any tester brief says the same. Every test or grade brief to a Codex worker says to run test commands outside its command sandbox (escalated). If it can't, it returns FAIL naming the sandbox, not the tests. Run it on a frozen tree, never the one you are still fixing: its subprocesses import from the tree for the whole run, so an edit under it voids the verdict. Use a worktree of the commit, or a scratch export (`C=$(mktemp -d); git ls-files -co --exclude-standard -z | rsync -a --from0 --files-from=- ./ "$C/"`), and keep fixing in the original.
- **Agent-primitive folders.** A change only under `.claude/`, `.codex/`, `.mentor/` or `.kiro/` runs no CI because both workflows skip it. Before pushing it, run `python3 -m unittest test_guide` with `PYTHONPATH=src:tests` and skill-creator's `quick_validate.py` on each edited skill.
- **Every failure blocks**, on every Python from 3.11 up, 3.15 included.
- **Unsure a failure is yours?** Run the same module in an export of the base (`C=$(mktemp -d); git archive origin/main | tar -x -C "$C"`), or in a scratch `git worktree add --detach` removed afterwards. Never stash the shared checkout for this.
- **A failure a rerun clears** is unexplained, not green. Before calling it flaky, rerunning or merging past it, answer: can the product hit this timing window? Presume a failure in code the change added is a product race until shown otherwise. Fix the product, then the test.
- **Mutation checks** (proving a test fails under a named mutation) run in a scratch copy under `$TMPDIR`, never in the shared working tree. The copy leaves out `.git`, `target`, `.mentor`, `node_modules` and any signing-key folder, and builds with its own `CARGO_TARGET_DIR`. It also leaves out `build/`, then brings back only what the checks need: a copy of `build/ui-dist` (the crate embeds it), links to the original's `build/node`, `build/ui-npm` and `build/cargo-home`, and the link `macos/ui/node_modules` → `../../build/ui-npm/node_modules`. Never link `build/` whole: the UI build writes `build/ui-dist`, so a mutant would reach the shared tree. A brief that asks for mutations names the copy and these rules. When a delegate dies mid-run, run `git diff` in the shared tree for leftover mutants before resuming.
- **Prove the new tests.** In a scratch copy (same rules), restore each changed source file from the base (`git show <base>:<file> > "$C/<file>"`; the base is `origin/main` for a PR, `HEAD` for a fix round) and run the new test ids. Each must fail by assertion. An import error or a missing module shows only that the code is new: report those apart from the catches. A test that still passes is fixed before it counts. A test that skips here counts once CI's log shows it ran (`gh run view <id> --log | grep <test name>` on the job that runs it). Code for a platform no CI job runs (Windows today) is named "not checked" in the PR body.
- **No test verdict while a release build runs.** Unchanged `client.rs` tests have failed and timed out during `build_app.sh`. Start the macOS tester after `build_app.sh` finishes. A red seen during a build is inconclusive: rerun it idle, and on `main`, before calling it flaky or blocking.
- **macOS app build names:** every build you make is `Agents Router Test.app`; only `scripts/build_app.sh` makes `Agents Router.app`. Locations and cleanup, data folders and login item included: `macos/README.md`, "Test and release builds". The exception: a hands-on check on the person's Mac uses the release build, installed over their copy with their OK, per [references/hands-on-check.md](references/hands-on-check.md).
- **macOS app:** the commands in `macos/README.md`. Every cargo call uses `CARGO_HOME=$PWD/build/cargo-home` (set from the repository root) and `--locked`. Build the UI first (`python3 scripts/build_app_ui.py`; the crate embeds `build/ui-dist`), then run `cargo test --locked` in `macos/src-tauri`, `node --test macos/ui/logic`, `python3 scripts/build_app_ui.py --test` and `python3 -m unittest test_app_sources` (`tests/test_app_sources.py`, the app's source guards, with `PYTHONPATH=src:tests`). Every app check set and every audit brief runs all four. Every test or grade brief to a Codex worker says to run test commands outside its command sandbox (escalated). If it can't, it returns FAIL naming the sandbox, not the tests. Installing a toolchain (Rust, or tauri-cli, Node and the UI's npm packages through `scripts/prepare_app_tools.sh`) needs the person's yes. To check a GUI fix by the person's click: [references/hands-on-check.md](references/hands-on-check.md).

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

**Above the newest tag.** Before merging any branch that changes shipped files, yours or someone else's, check that its version is above the newest release. Merging `main` can bring in a release of the same number with no conflict, and CI stays green, so only this check catches it. Run it from the branch's checkout:

```bash
git fetch -q --tags origin
new=$(gh release list --exclude-drafts --limit 1 --json tagName --jq '.[0].tagName'); new=${new#v}
cur=$(python3 -c 'import sys; sys.path.insert(0, "scripts"); from build_app import versions; print(versions())' | tail -1)
[ "$cur" != "$new" ] && [ "$(printf '%s\n%s\n' "$new" "$cur" | sort -V | tail -1)" = "$cur" ] && echo "above v$new" || echo "NOT above v$new"
git merge-base --is-ancestor "v$cur" HEAD; echo $?   # 0: v$cur is already released and in this branch; 1: it exists elsewhere; 128: no such tag yet
```

The version comparison decides; the ancestry line only shows why. Anything but "above" means the branch takes the next version.

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
- Before any sandbox job counts as evidence, a hand check's included, `agt-sandbox agt --version` must match `versions()` (§4). When it doesn't, run `scripts/sandbox.sh reinstall` first, from a checkout of the version under test: it installs agt from the checkout it runs in, and puts back the plugin from the marketplace source each profile already has.
- Work through the scenarios in [references/live-check.md](references/live-check.md).
- **Read-only exception on the person's real setup.** For a change to how agt learns Claude usage (status line, hook, transcript), also check their real setup with no job, no quota and no writes. Count which clients they use from their transcripts' `entrypoint` field. For each client that matters (an Orca chat, a terminal session idle for days, a fresh terminal reply), compare the popover with their own `/usage`. Do this before the tag, on a local release build installed with their OK ([hands-on-check §2](references/hands-on-check.md#2-build-and-install)). A sandbox check proves the mechanism only. A sandbox reply that hit a usage limit proves only the error path: record "normal path not checked", not PASS.
- Kiro is the exception: the sandbox logs it out, so it's checked with a scratch `AGT_HOME` on the real Kiro login ([references/live-check.md §7](references/live-check.md#7-kiro-outside-the-sandbox-with-a-scratch-agt-home)).
- Finish with `scripts/sandbox.sh destroy` when the person agrees.

## 6. Review

- **An independent pass.** Start `agents-router:agt-codex` with `access=read`. Give it the diff: `git diff main...HEAD` once the work is committed. Before that (the usual case, since you commit only when asked), run `git add -N <new files>` and give `git diff main`, which then includes the new files. Give it the plan too, never your conclusions, and ask for bugs and gaps.
  - A Codex read job has no network: it can't run `gh` or fetch a URL. Write every issue, PR body and follow-up issue it must read to a file beside its `cwd`, or paste it into the brief.
  - Re-review rounds may continue the same relay with each round's fix diff. The pass that clears a merge is a fresh job on the whole final diff, given the plan and no account of your fixes or "don't re-report" list.
- **Verifying an open PR before merge**, when the person asks you to check a PR against its issue and merge it:
  1. Make a detached worktree of the PR head under `$TMPDIR` (`git worktree add --detach "$WT" origin/<head>`) and merge `origin/main` into it. Never use the shared checkout.
  2. Run the full suite there (§3, "The full run"), prove the new tests (§3), and check the version is above the newest tag (§4).
  3. Run the independent pass and the security gate below, and the skill gate (release.md step 3) when a skill with eval cases changed.
  4. Push fixes to the PR head as a fast-forward (`git push origin HEAD:refs/heads/<head>`), never forced.
  5. Wait for CI to go green on the full head SHA (`gh pr view <n> --json headRefOid --jq .headRefOid`; a short SHA fails), merge with `gh pr merge <n> --merge --match-head-commit <that sha>`, then remove the worktree.
- **The security gate.** The repo has no CI security job, so when the person asks for one, it is these three parts, reported part by part and named as this skill's gate:
  - the independent pass, with a brief that names the attack classes for each new surface: path traversal, symlink swaps and TOCTOU, case and alias spellings, FIFOs, ReDoS, what workers can reach, forged events;
  - a probe test that reproduces each claimed blocker before you act on it;
  - a scan of the added lines (`git diff origin/main -- . ':!*.lock' | grep '^+'`) and `tests/fixtures/replay/` for keys, tokens, private keys, emails and home paths.
- **Open choices.** Settle them with the decision-maker agents (`system-decision-maker`, `ux-decision-maker`, `general-decision-maker`) instead of asking the person, unless the choice is theirs: scope, money or anything outward-facing.
- **Fold the findings in.** Then tell the person what you left out, and why. When their merge waits on "all gaps covered", bring them each item deferred beyond the scope they approved, and each choice that departs from the option they picked, before the merge, not in the summary after it. Adding to a follow-up issue after their approval is a scope change.

## 7. Release

Steps and exact commands: [references/release.md](references/release.md).

In short:
1. Run the full suite. When the change touches a skill with cases in `tests/skill-evals/`, that skill's graded cases must pass too, and only those (`--tag <skill>`, `-j 4`): the eval test, on the person's subscription quota: ask once, and never shape the work to avoid it. The benchmark and trigger tuning only advise. When the person asks for a security gate, run §6's.
2. Raise the version.
3. Commit with the subject `Agents Router X.Y.Z: <what changed for the person>`.
4. Push the branch and open a pull request. Then watch its CI: start `/mentor:merge`, or `gh pr checks <n> --watch` in the background. Triage a red job before the person has to report it.
5. After any merge, whoever merged (you, `/mentor:merge`, or the person), run `agt update --dry-run`. If it says the app manages agt, the merge reaches agt only through the app's update: don't run `agt update` before the person installs the release. After that install, confirm `plugins refreshed version=X.Y.Z` in the app's diagnostic log (`macos/README.md`, "Diagnostic log"), then ask for `/reload-plugins`. Otherwise run `agt update`, then ask for `/reload-plugins`. Before deferring over an unexpected dry-run line, check that state now; for the status line, use `agt doctor`'s live-usage line.
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
