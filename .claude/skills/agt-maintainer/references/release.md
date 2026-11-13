# Release

1. **Check the branch** with `git status` and `git log --oneline main..HEAD`. The tree should be clean, and you should not be on `main`.
2. **Run the full suite:**

   ```bash
   PYTHONPATH=src:tests python3 -m unittest discover -s tests -t tests
   ```

   It takes over 10 minutes; run it as SKILL.md §3 says (background, log, grep). The only failure allowed is `test_help.test_other_commands_keep_argparse_help` on Python 3.15.
3. **Pass the skill gate** when the change touches a skill with cases in `tests/skill-evals/`. Run only the cases of the skills it touched; they must pass before release.
   - Find the touched skills with `git diff --name-only main...HEAD`:

     | A changed path under | Tag |
     |---|---|
     | `skills/agents-router/` | `agents-router` |
     | `skills/agents-router-setup/` or `src/agents_router/guides/setup.md` | `agents-router-setup` |
     | `skills/orchestrator-setup/`, `skills/orchestrator-rebalance/`, `skills/orchestrator-customize/` | the folder's name |
     | `tests/skill-evals/<skill>/` | `<skill>` |

     Each case's `tags` start with its skill's name, and every case also has `release-gate`. Several `--tag` flags select every case that has any one of them, so never add `--tag release-gate` beside them: it selects all 25 cases.
   - **Clean export.** When the checkout holds build output (`macos/src-tauri/target`, `build/`), `claude plugin eval .` refuses it, and every case fails at $0, which looks like a skill failure. Run the load check and the graded run from a clean export, then use the same eval command:

     ```bash
     C=$(mktemp -d)
     git ls-files -co --exclude-standard -z | grep -zvE '^(\.mentor|\.kiro)/' | rsync -a --from0 --files-from=- ./ "$C/"
     cd "$C"
     ```

   - **Delegates.** Never move or delete build folders in a checkout a delegate is working in.
   - First check that every case loads. This runs no model and costs nothing:

     ```bash
     claude plugin eval . --eval-dir tests/skill-evals --case no-such-case --trust-plugin --no-publish --ablation none
     ```

     A case that fails to load is printed with `✗` and the reason; "No eval cases found matching" with no `✗` line means all loaded.
   - Then the graded run, one `--tag` per touched skill (here two). This is the eval test: real model runs on the person's subscription quota, three runs per case. Ask once before the first run (a plan approval that names it counts). Give a figure: cases × 3 runs × the per-case cost from the last run (or a `--runs 1` sample), plus one fix-and-rerun of a failing tag. Also say whether this session's own 5-hour window has room: the eval draws on the same subscription. It belongs to every release that touches a skill with cases: never shape a plan's scope, or escalate a question, to avoid it:

     ```bash
     PATH=".agt-eval/bin:<folder with a python3 ≥ 3.11, e.g. /opt/homebrew/bin>:$PATH" claude plugin eval . --eval-dir tests/skill-evals --tag agents-router --tag orchestrator-rebalance \
       -j 4 --scaffold --allow-tools Bash,Write,Edit,Agent --ablation none --no-publish --trust-plugin --keep-temp --output-dir "$TMPDIR/agt-skill-evals"
     ```

     `-j 4` runs four agent runs at once; they share one rate limit, and the report keeps the case order. Sequential (`-j 1`), all 25 cases take about 75 minutes. A Python 3.11+ folder comes right after `.agt-eval/bin`: each case's `place-files.sh` runs `python3 make_sandbox.py`, and the macOS system `python3` (3.9) fails every run with "scaffold failed" at no cost, which looks like a skill failure. `--trust-plugin` skips the first-run trust prompt, as in the load check. `--keep-temp` keeps each run's folder (printed as `kept temp: <dir>`) so you can read a failure. The run outlasts one shell call: start it in the background with its output in a log.

     `.agt-eval/bin` goes first on PATH so the runs' `agt` is the eval wrapper (`tests/skill-evals/bin/agt`, copied into each case's workspace, because a run can read only its workspace; the relative entry resolves there): each case's `place-files.sh` builds `.agt-eval/` in its own workspace (its own HOME, AGT_HOME, CLAUDE_CONFIG_DIR and CODEX_HOME, the fake CLIs, agt copied from this clone), and the wrapper refuses to run anywhere else. No run starts a real job or touches the real `~/.agt`.
   - A case that needs a role agent under its bare name, as agt installs it (`agt-reviewer`), keeps the stand-in in `<case>/profile-agents/`, with the description agt writes for that role (only the description routes; the body reviews the change itself). Its `place-files.sh` runs `tests/skill-evals/install-profile-agents.sh`, which copies the stand-in into the run's own Claude Code config folder, `agents/`: where agt writes it for a real profile. A run loads no `.claude/` folder from its workspace, even one a scaffold wrote, and an agent shipped in a test plugin would be named `<plugin>:agt-reviewer`, so this is the only way to the bare name we found. It isn't a documented eval feature (the eval docs say a scaffold supplies files and git state only). It relies on the run layout of Claude Code 2.1.316: `<run>/home` (with the workspace in it), `<run>/tmp`, and `<run>/config`, which exists before the scaffold runs. If that layout changes, the script exits 1 and the case's runs fail with "scaffold failed" instead of quietly running without the agent. To check a run, look for `agt-reviewer` in the `agents` list of the init event in its trace (`tracePath` in the JSON result).
   - **Writing graders.**
     1. Check what the agent ran from the wrapper's journal: a `regex` grader with `target: {source: file, path: .agt-eval/records/agt-calls.jsonl}` that matches the recorded argv and fields. Don't regex shell text in the trace (each new spelling escapes it or trips it), and don't ask an `llm` judge about commands.
     2. An `llm` grader with `focus: trace` sees only the start and end of a long trace (the first and last 12 events as of Claude Code 2.1.x). A command in the middle is invisible to it, and its votes flip between runs. Keep `llm` graders for judgement on a short target.
     3. Check wording with a `regex` on `last_message`, against a form the skill already prescribes (a sentence quoted word for word, an exact closing question). Rewording an `llm` rubric only moves the flake. Changing shipped copy just to give a grader something to match is a wording choice: settle it with `ux-decision-maker` (SKILL.md §6).
     4. A `regex` pattern is a JavaScript RegExp over compact JSON, one event per line. `match:` is `contains`, `not_contains` or `count:N`; `target:` is `trace`, `last_message`, `files` or `{source: file, path}`.
     5. For how `claude plugin eval` behaves, ask the `claude-code-guide` agent before searching the CLI binary.
   - Any case below 1.0 blocks the release; fix the skill, not the grader, unless the grader is wrong. A rerun that passes doesn't clear it, and no plan can grant a rerun allowance: a run below 1.0 is a real failure of the skill or the grader. Find the cause (When a case fails, below), fix it, and rerun that tag's three runs per case. The only other way to ship is the person's explicit waiver, named in the PR body.
   - **When a case fails**, before changing the skill or a grader:
     1. Run the same case on `main`, in a worktree off `main`. A case that also fails there is a harness, fixture or grader defect, not this change. Never decide that by comparing kept traces between branches: each trace follows the path its run took.
     2. Read the run. The console prints each kept run as `kept temp: <dir>`. The output folder's `aggregate-result.json` gives each run's `score`, `graders` and `tracePath` (as of Claude Code 2.1.x, under `.cases[].arms.with[]`). The final message is `jq -r 'select(.type=="result") | .result' <trace>`. The wrapper journal is `sealed/home/cwd/.agt-eval/records/agt-calls.jsonl` in the run folder; `chmod 700` the run folder and `sealed/` first.
     3. Check what the failing grader can see (Writing graders).
     4. Confirm the run loaded the skill from the checkout under test: grep the trace for `Base directory for this skill:`.
     5. A failed write under a workspace folder named `.claude` comes from the eval sandbox (EPERM in the trace), not the skill.
     6. Before the next graded run, replay a changed `regex` grader over the kept traces and final messages offline; it costs nothing.
   - While you edit a skill, a quicker loop is `--runs 1 -j 4` with that skill's `--tag`. It's advice only: the gate keeps three runs per case. `--case` keeps only the last flag; one case per invocation.
   - Advice only, never a gate: the with/without benchmark (drop `--ablation none`) and trigger tuning with skill-creator against `tests/skill-evals/<skill>/triggers.json`. Both use quota and vary from run to run, so they also stay out of the unit tests.
4. **Raise the version** in `.claude-plugin/plugin.json`, `pyproject.toml` and `src/agents_router/__init__.py` together:
   - a patch for fixes;
   - a minor version for new behavior.

   For a minor version, also update the guide's "This guide is for agt X.Y.Z or newer" sentence. Then run `python3 scripts/sync_guide.py` whenever the guide changed.
5. **Commit**, only when the person asks. The subject is `Agents Router X.Y.Z: <what changed, for the person>`, like the history (`git log --format=%s -10`). End the message with the attribution lines the session gives you.
6. **Push and open the pull request**, only when the person asks:

   ```bash
   git push -u origin HEAD
   gh pr create --title "Agents Router X.Y.Z: ..." --body "<summary, test results, live check>"
   ```

   End the body with the attribution footer the session gives you.
7. **After any merge, whoever merged** (you, `/mentor:merge`, or the person), first run `agt update --dry-run`, which prints where agt reinstalls from. It should name the remote (`git+https://…`). If it names a local checkout that holds uncommitted work, stop and ask the person. Then run `agt update` (it reinstalls from the recorded source and updates the plugin in every profile). Then tell the person to type `/reload-plugins`, or start new sessions, in each profile.
8. **macOS app release**, when the merged change ships the app:
   - **Merge vs. release.** A merge to `main` only uploads a workflow-run artifact. A GitHub release needs a strict `vX.Y.Z` tag on `main` after the three version files in step 4 agree and the app versions match. Tagging and publishing happen only when the person asks (`macos/README.md`, "Release build").
   - **Precondition.** The `release` environment exists and holds `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. Check at that scope with `gh api repos/<owner>/<repo>/environments/release/secrets --jq '.secrets[].name'`; `gh secret list` without `--env release` shows none of those environment secrets. Creating them is the person's step. Re-run the check after they say it's done, and again if a later change moves the scope.
   - **Check the draft.** After `draft-release`, the draft has four assets (`.dmg`, `.app.tar.gz`, `.sig`, `latest.json`), and `latest.json` names the version and the matching tag URL.
   - **Publish.** `gh release edit vX.Y.Z --draft=false --latest` runs only on the person's explicit word: every installed app follows `latest.json`. Then confirm that `https://github.com/fizzory/agents-router/releases/latest/download/latest.json` redirects (302).
   - **Close-out.** Until publication, the message after a merge names tag, secrets and publish as the remaining work.
