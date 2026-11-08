---
type: regex
target:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
match: contains
weight: 1
---

^(?![\s\S]*"kind"\s*:\s*"roles_config"[^\n]*"role"\s*:\s*"docs-writer"[^\n]*"writes"\s*:\s*true[^\n]*"successful_matching_apply_before"\s*:\s*false)[\s\S]*"exit_code"\s*:\s*0[^\n]*"kind"\s*:\s*"apply"[^\n]*"matches_proposal"\s*:\s*true
