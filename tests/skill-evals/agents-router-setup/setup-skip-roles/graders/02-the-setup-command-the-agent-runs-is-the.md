---
type: regex
target:
  source: file
  path: .agt-eval/records/agt-calls.jsonl
match: contains
weight: 1
---

^(?![\s\S]*"kind"\s*:\s*"apply"[^\n]*"roles_off"\s*:\s*false)(?![\s\S]*"kind"\s*:\s*"roles_config"[^\n]*"writes"\s*:\s*true)[\s\S]*"argv"\s*:\s*\[\s*"setup"[^\n\]]*(?:"--roles"\s*,\s*"off"|"--roles=off")
