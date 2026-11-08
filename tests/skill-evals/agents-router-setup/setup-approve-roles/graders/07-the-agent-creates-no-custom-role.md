---
type: regex
target:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
match: not_contains
weight: 1
---

"kind"\s*:\s*"roles_config"[^\n]*"writes"\s*:\s*true
