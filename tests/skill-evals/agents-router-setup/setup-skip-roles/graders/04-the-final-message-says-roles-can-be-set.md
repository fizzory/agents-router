---
type: regex
target: last_message
match: contains
flags: i
weight: 1
---

(?=[\s\S]*\broles\b)[\s\S]*?\b(?:you\s+can\s+set\s+(?:(?:roles|them)\s+up|up\s+(?:roles|them))|roles\s+can\s+be\s+set\s+up)\s+(?:later|(?:at\s+)?any\s+time(?:\s+later)?)\s+(?:by\s+asking\b|(?:with|using)\s+(?:the\s+)?`?(?:agents-router:)?orchestrator-setup\b)
