# Researka v2 Session Checklist

Use this at the top of every build session.

## Default v4 stack
Do these every time:
1. Constraint surface
2. Smallest design note
3. Deletion check
4. Change-impact map
5. Maker vs judge
6. One discriminating test
7. Output discipline

Do not run the full playbook by default.

## Paste-ready session block
```text
Researka v2 session.

Use the trimmed v4 toolkit, not the whole playbook.

Before coding, output only:
1. Goal
2. Non-goals
3. Constraint surface
4. Smallest viable design
5. Files to change
6. Change-impact map
7. Deletion check
8. Risks
9. What you refuse to build
10. One discriminating test

Rules:
- Keep backend-only boundaries intact
- One module = one reason to change
- Prefer explicit boring code over abstractions
- Do not add layers without hard necessity
- Keep the worker thin
- Keep business logic in runtime_core
- Keep contracts explicit
- If a simpler design exists, use it
- If a file starts becoming a god file, stop and split the responsibility

End every task with:
1. What changed
2. Why this was the minimal solution
3. What complexity was avoided
4. Remaining risk
5. Cheapest next validation step
6. Cleanliness score /10
```

## When to add more of v4

### Add these for medium-risk work
- Risk-tier routing
- Task-mode routing
- Decision journal entry

### Add these for high-blast-radius work
- Adversarial break-it pass
- Rollback mechanics
- Blast-fence plan
- Dependency audit
- Performance budget

### Weekly habit
- Ask: what can I delete, merge, or flatten this week?

## Researka v2 north star
- Small backend
- Clean contracts
- One vertical publishing slice at a time
- No frontend leakage
- No platform theater
