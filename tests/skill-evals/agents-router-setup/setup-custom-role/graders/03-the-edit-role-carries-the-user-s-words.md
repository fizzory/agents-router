---
type: llm
focus:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
weight: 1
---

The successful writing `agt roles config docs-writer` row (writes true, exit_code 0) carries `--user-said` in argv with the user's own words: they want a docs-writer role that updates the README and docs whenever a command or flag changes, touches nothing but docs and has edit access on Codex. It may be present up front or on agt's retry. A failed attempt without --user-said is allowed if the successful retry carries it.
