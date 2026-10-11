# Smart reorganization workflow

Use this when the user asks to "organize", "clean up", "sort into folders",
"restructure" their bookmarks. You (the model) decide the categories; the
script only executes a reviewed plan.

## 1. Snapshot and understand

```
python bmk.py info
python bmk.py stats --json
python bmk.py tree --folders-only
python bmk.py export --format json -o .bmk/export.json
```

Read `.bmk/export.json` (for very large libraries, read it in chunks or work
folder by folder with `--scope`). Each bookmark has `id`, `folder`, `name`,
`url`, `domain`, `added`, `last_used`.

## 2. Propose a structure (ask before acting)

Propose a short folder taxonomy to the user first, e.g.:

```
Bookmarks bar/
  Work/  Dev/  Docs/  Tools/
  Learning/  News/  Shopping/  Media/  Finance/  Personal/
Other bookmarks/
  Archive/        <- old / never-used items instead of deleting
```

Guidelines:
- 5-12 top-level folders; max depth 3; no folder with a single item unless it's
  clearly going to grow.
- Keep the user's existing folders that are already meaningful - don't churn.
- Prefer moving stale items (`last_used` empty and `added` > 1-2 years ago) to
  `Archive` over deleting.
- Clean up titles only when they are clearly bad: raw URLs, "Untitled",
  "Home", SEO-spam like "Buy cheap ... | Best ... 2019". Keep it short:
  "Site - Page" form.
- Never touch the root folders themselves.

## 3. Write a plan file

Write `.bmk/plan.json` (format: [plan-format.md](plan-format.md)). Order:
`mkdir` -> `move` -> `rename` / `set_url` -> `delete` -> `sort` -> `prune_empty`.

For large libraries, a domain-based rules file (`organize rules.json`, see
`examples/rules.example.json`) is a quick first pass; then handle the remainder
with a plan.

## 4. Preview -> confirm -> apply

```
python bmk.py apply .bmk/plan.json          # preview: prints every change
# show the user a summary (counts per folder, renames, deletions)
python bmk.py apply .bmk/plan.json --yes    # only after explicit approval
```

If it refuses because Chrome is running, ask the user to quit Chrome fully.

## 5. Verify and report

```
python bmk.py tree --depth 2
python bmk.py stats
```

Report: what moved where, how many renamed/deleted, and the backup path that
`apply` printed (undo with `python bmk.py restore --yes`).
