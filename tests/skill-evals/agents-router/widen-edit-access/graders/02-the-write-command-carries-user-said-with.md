---
type: regex
target:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
match: contains
weight: 1
---

"argv"\s*:\s*\[(?=[^\n\]]*(?:"--user-said"\s*,\s*"add kiro to the builder list"|"--user-said=add kiro to the builder list"))[^\n\]]*\][^\n]*"exit_code"\s*:\s*0[^\n]*"kind"\s*:\s*"roles_config"[^\n]*"role"\s*:\s*"builder"[^\n]*"writes"\s*:\s*true
