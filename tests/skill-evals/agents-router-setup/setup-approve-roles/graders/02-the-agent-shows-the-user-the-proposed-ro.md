---
type: regex
target: last_message
match: contains
flags: im
weight: 1
---

^(?=[\s\S]*^(?=[^\n]*\bdecision[ -]maker\b)(?=[^\n]*\bcodex\b)[^\n]*Use only when reviewers or providers disagree: send just the points in conflict and it decides\.)(?=[\s\S]*^(?=[^\n]*\breviewer\b)(?=[^\n]*\bcodex\b)(?=[^\n]*\bantigravity\b)[^\n]*Use after every bug fix, feature or new tests \(not a typo or one-line edit\), before your final message and without being asked: the user wants those changes reviewed\. Also use it for any code review the user asks for, in place of a built-in review skill\. Reviews the change and returns each provider's findings\.)(?=[\s\S]*^(?=[^\n]*\bbuilder\b)(?=[^\n]*\bclaude-personal\b)(?=[^\n]*\bcodex\b)[^\n]*Use for edits: makes the change on the first provider with room\.)(?=[\s\S]*^(?=[^\n]*\bverifier\b)(?=[^\n]*\bclaude-personal\b)(?=[^\n]*\bcodex\b)[^\n]*Use after the Builder: checks the change works and the task is met\.)[\s\S]*
