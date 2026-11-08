---
type: regex
target:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
match: contains
weight: 1
---

"exit_code"\s*:\s*0[^\n]*"kind"\s*:\s*"apply"[^\n]*"matches_proposal"\s*:\s*true
