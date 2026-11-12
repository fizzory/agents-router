---
name: agt-await-role-job
description: "Find and wait for the agt job behind an agt-* role agent you just started with the Agent tool, when you must hold the turn (mentor plan runs: 'find its job id in agt jobs, then run agt wait'). Covers matching the new job by role and start time, agt wait's 590 s bound, and macOS's missing timeout."
---

# Await an agt role job

Use this only when an instruction requires holding the turn on an `agt-*` role agent started with the Agent tool (the mentor-v6 rule). Otherwise follow the `agents-router` skill's notify flow: don't poll.

1. Before dispatching, save the workspace's `agt jobs` ids and the dispatch minute. Never use `--all`.
2. After dispatching, run one bounded `until` loop, no more than 590 s with a short `sleep` inside, never a leading `sleep N;`. It ends when a new id appears whose row shows `role <role> · g-…` and a created time at or after the dispatch minute. Match on that, not on a row position. If the deadline passes without a match, stop the loop and use the relay source in step 3.
3. If more than one new id matches (another session in the same workspace), don't guess. Wait for the relay's final line, which carries `· job <JOB> ·`, or take the id from the relay's result.
4. Run `agt wait --timeout 590 <id>`, repeating until the job is no longer running. Don't wrap it in `timeout`, which macOS lacks.
5. Get the result with `agt result <id> --json --max-chars 1000000 | jq -r .result`. Without `--max-chars`, the result stops at 20,000 characters with no warning.

| Pitfall | Instead |
|---|---|
| `sleep N; agt jobs` is blocked | Check immediately in the bounded loop; sleep only inside it. |
| `sed -n 3p` picks an old failed job | Match a new id by role and created time. |
| An unbounded `until` hangs past the shell's 600 s cap | End discovery within 590 s; use the relay if no match appears. |
| `timeout 580 agt wait` fails because macOS lacks GNU `timeout` | Use `agt wait --timeout 590 <id>`. |
| `agt jobs --all` includes another workspace's jobs | Save and compare workspace `agt jobs` ids. |
| Another session in the same workspace starts the same role | If multiple new ids match, take the id from your relay's final line or result. |
| `agt wait` defaults to 1800 s, longer than the shell cap | Pass `--timeout 590` on every wait. |
| `agt jobs --json` does not exist | Match the displayed role, id and created time; `--json` belongs on `agt result`. |
| A long result ends mid-sentence | Pass `--max-chars 1000000` to `agt result --json`. |
