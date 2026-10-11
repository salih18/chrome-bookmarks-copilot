# Chrome Bookmarks × GitHub Copilot

Let **GitHub Copilot (agent mode) in VS Code** manage your Chrome bookmarks:
organize into folders, remove duplicates, delete, rename, move, sort, find dead
links, renew moved URLs, and undo everything from automatic backups.

Works with **Chrome, Chrome Beta, Chromium, Brave and Edge** on Windows, macOS
and Linux. Pure Python 3.9+ standard library: nothing to install.

```
.github/
  copilot-instructions.md              # always-on workspace rules for Copilot
  skills/chrome-bookmarks/
    SKILL.md                           # the Agent Skill Copilot loads on demand
    scripts/bookmarks.py               # the engine (CLI)
    references/organize-workflow.md    # how Copilot should reorganize
    references/plan-format.md          # batch plan JSON format
  agents/bookmark-manager.agent.md     # "Bookmark Manager" custom agent
  prompts/                             # /organize-bookmarks, /cleanup-bookmarks,
                                       # /check-links, /bookmark
.vscode/settings.json                  # enables skills, auto-approves read-only cmds
bmk.py                                 # shortcut: python bmk.py <command>
examples/                              # rules + plan examples
tests/                                 # python -m unittest discover -s tests -v
```

## Setup

1. Install Python 3.9+ (`python --version`).
2. Open this folder in VS Code with GitHub Copilot Chat enabled.
3. Open Copilot Chat and switch to **Agent** mode, or pick the
   **Bookmark Manager** agent from the agent dropdown.
4. Sanity check in a terminal:
   ```
   python bmk.py info
   python bmk.py profiles      # if you use several Chrome profiles
   ```

## Using it from Copilot Chat

Slash prompts:

| Prompt | What it does |
|---|---|
| `/organize-bookmarks` | analyzes everything, proposes a folder structure, applies after your OK |
| `/cleanup-bookmarks` | duplicates, empty folders, ugly titles |
| `/check-links` | dead-link scan, renews redirected URLs, offers to remove dead ones |
| `/bookmark` | quick one-off task (add / rename / move / delete / find) |

You can also just ask, e.g. *"move all my GitHub bookmarks into Dev/Code"*,
*"delete everything about my old job at Contoso"*, or *"rename bookmarks that
are just raw URLs"*.

Copilot always **previews first** and asks before any bulk or destructive change.

## Important: close Chrome before changes

Chrome holds bookmarks in memory and would overwrite edits made while it runs,
so the script **refuses to write while the browser is running**. Quit Chrome
completely, including the tray icon and "Continue running background apps when
Chrome is closed". Read-only commands (`tree`, `search`, `stats`, `check` ...)
work any time.

## Using it directly (CLI)

```bash
python bmk.py tree --depth 2
python bmk.py search "react hooks"
python bmk.py add https://react.dev --name "React" --folder "bar/Dev/Docs" --create
python bmk.py rename 123 "Better title"
python bmk.py move 123 124 --to "bar/Dev/Docs"
python bmk.py delete 130            # preview
python bmk.py delete 130 --yes      # apply
python bmk.py dedupe --yes
python bmk.py prune --yes           # empty folders
python bmk.py sort bar -r
python bmk.py check -o .bmk/report.json
python bmk.py renew --report .bmk/report.json --https --yes
python bmk.py organize examples/rules.example.json --sort --yes
python bmk.py apply .bmk/plan.json --yes
python bmk.py restore --yes         # undo: restore the latest backup
```

Address items by **id** (shown in `tree` / `list`) or **path** like
`"Bookmarks bar/Dev"`. Path aliases `bar`, `other` and `mobile` work in any
language. Run `python bmk.py <command> -h` for all options.

Options: `--browser {chrome,chrome-beta,chromium,brave,edge}`,
`--profile "Profile 1"`, `--file <path>`. The env vars `BMK_BROWSER`,
`BMK_PROFILE`, `BMK_FILE` and `BMK_BACKUP_DIR` set the same things.

**WSL users:** point at the Windows file, e.g.
`BMK_FILE="/mnt/c/Users/<you>/AppData/Local/Google/Chrome/User Data/Default/Bookmarks"`.
The "is Chrome running" check can't see Windows processes from WSL, so make
sure Chrome is closed yourself.

## Safety

- A timestamped backup goes to `~/.bmk-backups/` before every write (the last
  50 are kept). Use `backups` to list them and `restore` to roll back.
- Chrome's MD5/SHA-256 checksums are recomputed on save. This was verified
  against a real Chrome 146: Chrome-written checksums match, and Chrome loads
  CLI edits (unicode titles included).
- Writes are atomic (temp file + rename), and `apply` plans are all-or-nothing.
- `check` only marks a link **dead** on 404/410 or a domain that doesn't exist.
  Login walls, bot blocks and timeouts are reported as `blocked`/`error` and
  never auto-deleted.

## Chrome Sync

If `info` shows `sync_metadata_present: true`, the profile syncs to your Google
account. Edits made while Chrome is closed are loaded fine locally, but Sync can
reconcile them with the server copy on next start, so some renames or deletions
**might be reverted, or deleted items may come back**. Before a big
reorganization, test with one small change: apply it, open Chrome, wait a
minute, and check that it stuck. If Sync fights you, the robust fix is a small
companion Chrome extension using the `chrome.bookmarks` API (sync-aware). That
isn't included yet.

## Development

```
python -m unittest discover -s tests -v
```
