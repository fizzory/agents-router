---
type: regex
target:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
match: contains
weight: 1
---

"argv"\s*:\s*\[(?=[^\n\]]*"--dry-run")(?=[^\n\]]*"--json")[^\n\]]*\][^\n]*"exit_code"\s*:\s*0[^\n]*"kind"\s*:\s*"dry_run"[^\n]*"before_any_apply"\s*:\s*true[^\n]*"command"\s*:\s*"agt setup\s
