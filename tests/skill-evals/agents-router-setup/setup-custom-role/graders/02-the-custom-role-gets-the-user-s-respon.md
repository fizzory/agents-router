---
type: llm
focus:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
weight: 1
---

A journal row with kind roles_config, role docs-writer, writes true and exit_code 0 has argv for a non-dry-run `agt roles config docs-writer` with `--access edit`, a Codex provider and a `--description` that says the role updates the README and docs whenever a command or flag changes and touches nothing but docs (the user's words or the same meaning, not a responsibility the user didn't give). A failed or dry-run attempt does not count.
