# Plan file format (`apply`)

A plan is JSON: either `{"operations": [...]}` or a bare list. Operations run
in order, in memory. If **any** operation fails (unknown id, ambiguous path,
moving a folder into itself, ...) the whole plan is rejected and nothing is
written.

`id` (or `ref`) accepts an id (`"42"`, `"id:42"`) or a path. Folder paths
start with a root: `bar` / `other` / `mobile` (or the real localized names).

| op | fields | notes |
|---|---|---|
| `mkdir` | `path` | creates missing parents (mkdir -p) |
| `add` | `url`, `folder`, `name?`, `index?` | folder is created if missing |
| `move` | `id`, `to`, `index?` | `to` path is created if missing; ids also allowed |
| `rename` | `id`, `name` | bookmarks or folders |
| `set_url` | `id`, `url` | bookmarks only |
| `delete` | `id` | folders are deleted recursively |
| `sort` | `folder`, `by?` (`name`/`url`/`date`/`domain`), `folders_first?` (true), `recursive?` (false) | |
| `prune_empty` | `scope?` | removes empty folders (deepest first) |

Example:

```json
{
  "operations": [
    {"op": "mkdir", "path": "bar/Dev/Docs"},
    {"op": "move", "id": "123", "to": "bar/Dev/Docs"},
    {"op": "move", "id": "124", "to": "bar/Dev/Docs"},
    {"op": "rename", "id": "124", "name": "MDN - Fetch API"},
    {"op": "set_url", "id": "130", "url": "https://new.example.com/page"},
    {"op": "move", "id": "140", "to": "other/Archive"},
    {"op": "delete", "id": "151"},
    {"op": "sort", "folder": "bar/Dev", "recursive": true},
    {"op": "prune_empty"}
  ]
}
```

Tips:
- Moving many items: one `move` op per id (keeps errors precise).
- Deleting a folder deletes everything in it. List its contents to the user first.
- Keep plans in `.bmk/` (git-ignored) so they don't get committed.
