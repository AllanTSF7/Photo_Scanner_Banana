---
name: web-design-guidelines
description: Review UI code for Web Interface Guidelines compliance. Use when asked to "review my UI", "check accessibility", "audit design", "review UX", or "check my site against best practices".
metadata:
  author: vercel
  version: "1.0.0"
  argument-hint: <file-or-pattern>
---

# Web Interface Guidelines

Review files for compliance with Web Interface Guidelines.

## Project notes (Photo Scanner) - read first

- Vendored and **pinned**: the rules live in `references/command.md` in this folder (see SOURCE.txt). Do NOT fetch them
  from the network; read the local file. Update it only by re-vendoring after review.
- CLAUDE.md > UI DESIGN SYSTEM wins on conflicts. In this project: rules about CDN `preconnect`, React hydration,
  `nuqs`/router state and Tailwind class names don't apply (plain offline HTML/CSS/JS). The existing UI uses **sentence
  case** labels ("Export approved", "Flag duplicate"): report Title Case as a note, don't mass-rewrite labels unasked.
- Default scope when no files are given: `banana/web/static/index.html`, `app.css`, `app.js`, `theme.css`.
- Findings are a review; fixes still have to keep `tests/test_ui_rules.py` and `tests/ui/test_ui_live.py` passing.

## How It Works

1. Read the guidelines from `references/command.md`
2. Read the specified files (or use the default scope above)
3. Check against all rules in the guidelines
4. Output findings in the terse `file:line` format

## Usage

When a user provides a file or pattern argument:
1. Read guidelines from `references/command.md`
2. Read the specified files
3. Apply all rules from the guidelines
4. Output findings using the format specified in the guidelines