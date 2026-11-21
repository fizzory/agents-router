---
type: regex
target: last_message
match: contains
flags: i
weight: 1
---

(?<![a-z0-9])when\s+you\s+want\s+roles,?\s+say\s+[*_'"‘’“”`]{0,3}set\s+up\s+my\s+roles(?![a-z0-9])
