#!/usr/bin/env python3
"""
bookmarks.py - manage Chrome / Chromium / Brave / Edge bookmarks from the CLI.

Standard library only (Python 3.9+). Works on Windows, macOS and Linux by
editing the browser's `Bookmarks` JSON file directly.

Safety model
  * The browser MUST be fully closed before any write. Chrome keeps bookmarks
    in memory and would overwrite (or ignore) changes made while it runs.
  * Every write makes a timestamped backup first (see `backups` / `restore`).
  * Chrome's MD5 (and SHA-256) checksums are recomputed on every save.
  * The file is replaced atomically (temp file + rename).
  * Bulk / destructive commands (delete, dedupe, prune, renew, organize,
    apply, restore) only PREVIEW unless you pass --yes.
  * Every write command accepts --dry-run.

Run `python bookmarks.py -h` or `python bookmarks.py <command> -h` for help.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

WEBKIT_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01
ROOT_KEYS = ("bookmark_bar", "other", "synced")  # order matters for the checksum
ROOT_ALIASES = {
    "bookmark_bar": "bookmark_bar", "bar": "bookmark_bar", "bookmarks bar": "bookmark_bar",
    "bookmarks_bar": "bookmark_bar", "toolbar": "bookmark_bar", "favorites bar": "bookmark_bar",
    "other": "other", "other bookmarks": "other", "other favorites": "other",
    "synced": "synced", "mobile": "synced", "mobile bookmarks": "synced",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAX_BACKUPS = 50

BROWSERS = {
    "chrome": {
        "linux": "~/.config/google-chrome",
        "darwin": "~/Library/Application Support/Google/Chrome",
        "win32": r"%LOCALAPPDATA%\Google\Chrome\User Data",
        "proc": {"linux": ["chrome"], "darwin": ["Google Chrome"], "win32": ["chrome.exe"]},
    },
    "chrome-beta": {
        "linux": "~/.config/google-chrome-beta",
        "darwin": "~/Library/Application Support/Google/Chrome Beta",
        "win32": r"%LOCALAPPDATA%\Google\Chrome Beta\User Data",
        "proc": {"linux": ["chrome"], "darwin": ["Google Chrome Beta"], "win32": ["chrome.exe"]},
    },
    "chromium": {
        "linux": "~/.config/chromium",
        "darwin": "~/Library/Application Support/Chromium",
        "win32": r"%LOCALAPPDATA%\Chromium\User Data",
        "proc": {"linux": ["chromium", "chromium-browse"], "darwin": ["Chromium"], "win32": ["chromium.exe"]},
    },
    "brave": {
        "linux": "~/.config/BraveSoftware/Brave-Browser",
        "darwin": "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "win32": r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
        "proc": {"linux": ["brave"], "darwin": ["Brave Browser"], "win32": ["brave.exe"]},
    },
    "edge": {
        "linux": "~/.config/microsoft-edge",
        "darwin": "~/Library/Application Support/Microsoft Edge",
        "win32": r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
        "proc": {"linux": ["msedge"], "darwin": ["Microsoft Edge"], "win32": ["msedge.exe"]},
    },
}


class BmkError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #

def platform_key() -> str:
    if sys.platform.startswith("win") or sys.platform == "cygwin":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def user_data_dir(browser: str) -> Path:
    raw = BROWSERS[browser][platform_key()]
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def bookmarks_file(args) -> Path:
    explicit = getattr(args, "file", None) or os.environ.get("BMK_FILE")
    if explicit:
        return Path(explicit).expanduser()
    return user_data_dir(args.browser) / args.profile / "Bookmarks"


def browser_running(browser: str) -> bool | None:
    """True/False, or None if we could not tell."""
    plat = platform_key()
    names = BROWSERS[browser]["proc"][plat]
    try:
        if plat == "win32":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15
            ).stdout.lower()
            return any(f'"{n.lower()}"' in out for n in names)
        for n in names:
            if subprocess.run(["pgrep", "-x", n], capture_output=True, timeout=15).returncode == 0:
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return None


def now_webkit() -> str:
    return str(int((time.time() + WEBKIT_EPOCH_OFFSET) * 1_000_000))


def webkit_to_iso(value) -> str | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    secs = v / 1_000_000 - WEBKIT_EPOCH_OFFSET
    return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def canonical_url(url: str) -> str:
    """Light GURL-style canonicalisation so Chrome's own checksum matches ours."""
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    scheme = p.scheme.lower()
    if scheme in ("http", "https"):
        netloc = p.netloc.lower()
        path = p.path or "/"
        return urllib.parse.urlunsplit((scheme, netloc, path, p.query, p.fragment))
    return url


def domain_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_for_dedupe(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    if p.scheme.lower() not in ("http", "https"):
        return url.strip()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if p.port and p.port not in (80, 443):
        host += f":{p.port}"
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid", "ref")]
    path = p.path.rstrip("/") or ""
    return f"{host}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")


# --------------------------------------------------------------------------- #
# Checksum (mirrors components/bookmarks/browser/bookmark_codec.cc)
# --------------------------------------------------------------------------- #

def compute_checksums(data: dict) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()

    def upd(b: bytes):
        md5.update(b)
        sha.update(b)

    def walk(n: dict):
        upd(n["id"].encode("utf-8"))
        upd(n.get("name", "").encode("utf-16-le"))
        if n.get("type") == "url":
            upd(b"url")
            upd(n.get("url", "").encode("utf-8"))
        else:
            upd(b"folder")
            for c in n.get("children", []):
                walk(c)

    for key in ROOT_KEYS:
        if isinstance(data["roots"].get(key), dict):
            walk(data["roots"][key])
    return md5.hexdigest(), sha.hexdigest()


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #

def backup_dir() -> Path:
    return Path(os.environ.get("BMK_BACKUP_DIR", "~/.bmk-backups")).expanduser()


def backup_prefix(path: Path) -> str:
    # e.g. "Chrome-Default" / "User Data-Profile 1"
    return re.sub(r"[^\w.-]+", "_", f"{path.parent.parent.name}-{path.parent.name}")


def make_backup(path: Path, reason: str = "auto") -> Path:
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = d / f"{backup_prefix(path)}__{stamp}__{reason}.json"
    shutil.copy2(path, dest)
    olds = sorted(d.glob(f"{backup_prefix(path)}__*.json"))
    for old in olds[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def list_backups(path: Path) -> list[Path]:
    return sorted(backup_dir().glob(f"{backup_prefix(path)}__*.json"))


# --------------------------------------------------------------------------- #
# Bookmark model
# --------------------------------------------------------------------------- #

class Bookmarks:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise BmkError(
                f"Bookmarks file not found: {self.path}\n"
                "Use --browser/--profile (see `profiles`) or --file / BMK_FILE."
            )
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)
        if "roots" not in self.data:
            raise BmkError(f"{self.path} does not look like a Chromium Bookmarks file")
        self.changes: list[str] = []
        self.reindex()

    # ---- indexing -------------------------------------------------------- #
    def roots(self) -> list[dict]:
        return [self.data["roots"][k] for k in ROOT_KEYS if isinstance(self.data["roots"].get(k), dict)]

    def reindex(self):
        self.nodes: dict[str, dict] = {}
        self.parents: dict[str, dict | None] = {}

        def walk(n, parent):
            self.nodes[n["id"]] = n
            self.parents[n["id"]] = parent
            for c in n.get("children", []):
                walk(c, n)

        for r in self.roots():
            walk(r, None)

    def is_root(self, node: dict) -> bool:
        return self.parents.get(node["id"]) is None

    def next_id(self) -> str:
        return str(max(int(i) for i in self.nodes) + 1)

    def path_parts(self, node: dict) -> list[str]:
        parts = []
        n = node
        while n is not None:
            parts.append(n.get("name", ""))
            n = self.parents.get(n["id"])
        return list(reversed(parts))

    def path_of(self, node: dict) -> str:
        return "/".join(self.path_parts(node))

    def folder_path_of(self, node: dict) -> str:
        parent = self.parents.get(node["id"])
        return self.path_of(parent) if parent else ""

    def iter_nodes(self, start: dict | None = None):
        starts = [start] if start else self.roots()

        def walk(n):
            yield n
            for c in n.get("children", []):
                yield from walk(c)

        for s in starts:
            yield from walk(s)

    def iter_urls(self, start=None):
        return (n for n in self.iter_nodes(start) if n.get("type") == "url")

    def iter_folders(self, start=None):
        return (n for n in self.iter_nodes(start) if n.get("type") == "folder")

    # ---- resolution ------------------------------------------------------ #
    def _root_for(self, name: str) -> dict | None:
        key = ROOT_ALIASES.get(name.lower())
        if key and isinstance(self.data["roots"].get(key), dict):
            return self.data["roots"][key]
        for r in self.roots():
            if r.get("name", "").lower() == name.lower():
                return r
        return None

    def resolve(self, ref: str, *, want: str | None = None) -> dict:
        """Resolve an id ("42" / "id:42") or a path ("Bookmarks bar/Dev/Tools")."""
        ref = str(ref).strip()
        if ref.startswith("id:"):
            ref = ref[3:]
        if ref.isdigit():
            node = self.nodes.get(ref)
            if not node:
                raise BmkError(f"No bookmark or folder with id {ref}")
        else:
            node = self._resolve_path(ref)
        if want and node.get("type") != want:
            raise BmkError(f"{ref!r} is a {node.get('type')}, expected a {want}")
        return node

    def _resolve_path(self, ref: str) -> dict:
        parts = [p for p in ref.strip("/").split("/") if p]
        if not parts:
            raise BmkError("Empty path")
        node = self._root_for(parts[0])
        if node is None:
            names = ", ".join(r.get("name", "") for r in self.roots())
            raise BmkError(f"Unknown root {parts[0]!r}. Roots: {names} (aliases: bar, other, mobile)")
        for part in parts[1:]:
            kids = node.get("children", [])
            matches = [c for c in kids if c.get("name") == part] or \
                      [c for c in kids if c.get("name", "").lower() == part.lower()]
            folders = [c for c in matches if c.get("type") == "folder"]
            if folders:
                matches = folders
            if not matches:
                raise BmkError(f"Not found: {part!r} in {self.path_of(node)!r}")
            if len(matches) > 1:
                ids = ", ".join(c["id"] for c in matches)
                raise BmkError(f"Ambiguous {part!r} in {self.path_of(node)!r} (ids {ids}); use an id")
            node = matches[0]
        return node

    # ---- mutations ------------------------------------------------------- #
    def _touch(self, folder: dict):
        folder["date_modified"] = now_webkit()

    def _check_folder(self, folder: dict):
        if folder.get("type") != "folder":
            raise BmkError(f"{self.path_of(folder)!r} is not a folder")

    def ensure_folder(self, path: str) -> dict:
        """mkdir -p. Returns the (possibly new) folder."""
        parts = [p for p in path.strip("/").split("/") if p]
        node = self._root_for(parts[0]) if parts else None
        if node is None:
            raise BmkError(f"Path must start with a root (bar / other / mobile): {path!r}")
        for part in parts[1:]:
            existing = [c for c in node.get("children", [])
                        if c.get("type") == "folder" and c.get("name", "").lower() == part.lower()]
            node = existing[0] if existing else self.add_folder(node, part)
        return node

    def add_folder(self, parent: dict, name: str, index: int | None = None) -> dict:
        self._check_folder(parent)
        ts = now_webkit()
        node = {
            "children": [], "date_added": ts, "date_last_used": "0", "date_modified": ts,
            "guid": str(uuid.uuid4()), "id": self.next_id(), "name": name, "type": "folder",
        }
        self._insert(parent, node, index)
        self.changes.append(f"mkdir  [{node['id']}] {self.path_of(node)}")
        return node

    def add_url(self, parent: dict, url: str, name: str | None = None, index: int | None = None) -> dict:
        self._check_folder(parent)
        url = canonical_url(url)
        node = {
            "date_added": now_webkit(), "date_last_used": "0", "guid": str(uuid.uuid4()),
            "id": self.next_id(), "name": name or url, "type": "url", "url": url,
        }
        self._insert(parent, node, index)
        self.changes.append(f"add    [{node['id']}] {self.path_of(node)} -> {url}")
        return node

    def _insert(self, parent: dict, node: dict, index: int | None):
        kids = parent.setdefault("children", [])
        kids.insert(len(kids) if index is None else max(0, min(index, len(kids))), node)
        self.nodes[node["id"]] = node
        self.parents[node["id"]] = parent
        self._touch(parent)

    def rename(self, node: dict, name: str):
        if self.is_root(node):
            raise BmkError("Root folders cannot be renamed (Chrome resets their names)")
        old = node.get("name", "")
        node["name"] = name
        self.changes.append(f"rename [{node['id']}] {old!r} -> {name!r}")

    def set_url(self, node: dict, url: str):
        if node.get("type") != "url":
            raise BmkError("set-url only works on bookmarks, not folders")
        old = node["url"]
        node["url"] = canonical_url(url)
        self.changes.append(f"seturl [{node['id']}] {old} -> {node['url']}")

    def move(self, node: dict, folder: dict, index: int | None = None):
        self._check_folder(folder)
        if self.is_root(node):
            raise BmkError("Root folders cannot be moved")
        n = folder
        while n is not None:
            if n is node:
                raise BmkError("Cannot move a folder into itself or one of its subfolders")
            n = self.parents.get(n["id"])
        old_parent = self.parents[node["id"]]
        old_parent["children"].remove(node)
        self._touch(old_parent)
        self._insert(folder, node, index)
        self.changes.append(f"move   [{node['id']}] {node.get('name')!r}: "
                            f"{self.path_of(old_parent)} -> {self.path_of(folder)}")

    def delete(self, node: dict):
        if self.is_root(node):
            raise BmkError("Root folders cannot be deleted")
        parent = self.parents[node["id"]]
        desc = self.path_of(node)
        count = sum(1 for _ in self.iter_urls(node)) if node.get("type") == "folder" else 1
        parent["children"].remove(node)
        self._touch(parent)
        for n in list(self.iter_nodes(node)):
            self.nodes.pop(n["id"], None)
            self.parents.pop(n["id"], None)
        extra = f" ({count} bookmarks inside)" if node.get("type") == "folder" else f" {node.get('url')}"
        self.changes.append(f"delete [{node['id']}] {desc}{extra}")

    def sort(self, folder: dict, by: str = "name", folders_first: bool = True, recursive: bool = False):
        self._check_folder(folder)
        keyfn = {
            "name": lambda n: n.get("name", "").casefold(),
            "url": lambda n: n.get("url", "").casefold(),
            "date": lambda n: int(n.get("date_added", "0") or 0),
            "domain": lambda n: (domain_of(n.get("url", "")), n.get("name", "").casefold()),
        }[by]
        kids = folder.get("children", [])
        before = [c["id"] for c in kids]
        if folders_first:
            # folders are ordered by name unless sorting by date; bookmarks by the chosen key
            fkey = keyfn if by in ("name", "date") else (lambda n: n.get("name", "").casefold())
            kids.sort(key=lambda n: (0, fkey(n), "") if n.get("type") == "folder" else (1, keyfn(n), ""))
        else:
            kids.sort(key=keyfn)
        if [c["id"] for c in kids] != before:
            self._touch(folder)
            self.changes.append(f"sort   [{folder['id']}] {self.path_of(folder)} by {by}")
        if recursive:
            for c in kids:
                if c.get("type") == "folder":
                    self.sort(c, by, folders_first, True)

    # ---- persistence ----------------------------------------------------- #
    def save(self, *, browser: str, force: bool = False, reason: str = "auto") -> Path:
        if not force:
            running = browser_running(browser)
            if running:
                raise BmkError(
                    f"{browser} is running. Quit it completely (including background/tray "
                    "processes) and retry. Changes made while it runs would be lost."
                )
            if running is None:
                warn("Could not determine whether the browser is running - make sure it is closed.")
        backup = make_backup(self.path, reason)
        md5, sha = compute_checksums(self.data)
        self.data["checksum"] = md5
        if "checksum_sha256" in self.data:
            self.data["checksum_sha256"] = sha
        tmp = self.path.with_name(self.path.name + ".bmk-tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, indent=3, ensure_ascii=False)
        os.replace(tmp, self.path)
        return backup


# --------------------------------------------------------------------------- #
# Link checking
# --------------------------------------------------------------------------- #

class _RecordRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.chain: list[tuple[int, str]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SUSPICIOUS_REDIRECT = re.compile(r"(login|signin|sign-in|sign_in|auth|consent|sso|captcha|account)", re.I)


def check_url(url: str, timeout: float = 10.0) -> dict:
    res = {"url": url, "status": None, "final_url": url, "redirects": [], "verdict": "error", "error": None}
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        res["verdict"] = "skipped"
        return res
    for method in ("HEAD", "GET"):
        handler = _RecordRedirects()
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                res["status"], res["final_url"] = resp.status, resp.geturl()
            res["error"] = None
        except urllib.error.HTTPError as e:
            res["status"], res["final_url"] = e.code, (e.geturl() or url)
            res["error"] = None
            if method == "HEAD":
                continue  # many servers mishandle HEAD; confirm with GET
        except urllib.error.URLError as e:
            reason = e.reason
            res["error"] = f"{type(reason).__name__}: {reason}"
            if isinstance(reason, socket.gaierror):
                res["verdict"] = "dead"  # DNS name does not resolve
                return res
            if method == "HEAD":
                continue
            return res
        except (TimeoutError, socket.timeout, ConnectionError, ssl.SSLError,
                http.client.HTTPException, ValueError, OSError) as e:
            res["error"] = f"{type(e).__name__}: {e}"
            if method == "HEAD":
                continue
            return res
        res["redirects"] = handler.chain
        break

    st = res["status"]
    if st is None:
        res["verdict"] = "error"
    elif st in (404, 410):
        res["verdict"] = "dead"
    elif st in (401, 403, 429, 451):
        res["verdict"] = "blocked"  # can't tell; bot protection / login wall
    elif st >= 400:
        res["verdict"] = "error"
    elif res["redirects"] and res["final_url"].rstrip("/") != url.rstrip("/"):
        res["verdict"] = "redirect"
    else:
        res["verdict"] = "ok"

    if res["verdict"] == "redirect":
        codes = [c for c, _ in res["redirects"]]
        res["permanent"] = all(c in (301, 308) for c in codes)
        old, new = urllib.parse.urlsplit(url), urllib.parse.urlsplit(res["final_url"])
        res["suspicious"] = bool(
            SUSPICIOUS_REDIRECT.search(new.path + "?" + new.query) and not SUSPICIOUS_REDIRECT.search(old.path)
        ) or (new.path in ("", "/") and old.path not in ("", "/"))
    return res


def run_checks(bm: Bookmarks, scope: dict | None, timeout: float, workers: int, progress=True) -> list[dict]:
    nodes = [n for n in bm.iter_urls(scope)]
    results = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_url, n["url"], timeout): n for n in nodes}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            n = futs[fut]
            r = fut.result()
            r.update({"id": n["id"], "name": n.get("name", ""), "folder": bm.folder_path_of(n)})
            results.append(r)
            if progress and sys.stderr.isatty():
                print(f"\r  checked {i}/{len(nodes)}", end="", file=sys.stderr, flush=True)
    if progress and sys.stderr.isatty():
        print(file=sys.stderr)
    checked = [r for r in results if r["verdict"] != "skipped"]
    if len(checked) >= 5 and sum(r["verdict"] in ("dead", "error") for r in checked) > 0.5 * len(checked):
        warn("More than half of the links failed - check your internet connection/VPN/proxy "
             "before trusting these results.")
    order = {"dead": 0, "error": 1, "blocked": 2, "redirect": 3, "ok": 4, "skipped": 5}
    results.sort(key=lambda r: (order.get(r["verdict"], 9), r["folder"], r["name"]))
    return results


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def warn(msg: str):
    print(f"warning: {msg}", file=sys.stderr)


def out_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def node_summary(bm: Bookmarks, n: dict) -> dict:
    d = {"id": n["id"], "type": n.get("type"), "name": n.get("name", ""), "folder": bm.folder_path_of(n)}
    if n.get("type") == "url":
        d["url"] = n.get("url", "")
        d["domain"] = domain_of(d["url"])
        d["added"] = webkit_to_iso(n.get("date_added"))
        d["last_used"] = webkit_to_iso(n.get("date_last_used"))
    else:
        d["path"] = bm.path_of(n)
        d["bookmarks"] = sum(1 for _ in bm.iter_urls(n))
        d["subfolders"] = sum(1 for c in n.get("children", []) if c.get("type") == "folder")
    return d


def finish(bm: Bookmarks, args, *, needs_yes: bool = False, reason: str = "auto"):
    """Print the change list and save (unless dry-run / preview)."""
    if not bm.changes:
        print("No changes.")
        return
    for c in bm.changes:
        print(c)
    preview = args.dry_run or (needs_yes and not getattr(args, "yes", False))
    if preview:
        hint = "" if args.dry_run else "  Re-run with --yes to apply."
        print(f"\nPREVIEW ONLY - {len(bm.changes)} change(s), nothing written.{hint}")
        return
    backup = bm.save(browser=args.browser, force=args.force, reason=reason)
    print(f"\nSaved {len(bm.changes)} change(s) to {bm.path}\nBackup: {backup}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_profiles(args):
    base = user_data_dir(args.browser)
    profiles = []
    try:
        with open(base / "Local State", encoding="utf-8") as f:
            cache = json.load(f).get("profile", {}).get("info_cache", {})
        for d, info in cache.items():
            profiles.append({"dir": d, "name": info.get("name"), "user": info.get("user_name") or None,
                             "has_bookmarks": (base / d / "Bookmarks").exists()})
    except FileNotFoundError:
        for p in sorted(base.glob("*/Bookmarks")):
            profiles.append({"dir": p.parent.name, "name": None, "user": None, "has_bookmarks": True})
    if args.json:
        return out_json({"user_data_dir": str(base), "profiles": profiles})
    print(f"User data dir: {base}")
    if not profiles:
        print("  (no profiles found)")
    for p in profiles:
        flag = "" if p["has_bookmarks"] else "  (no Bookmarks file)"
        print(f"  --profile {p['dir']!r:<14} {p['name'] or ''} {('<' + p['user'] + '>') if p['user'] else ''}{flag}")


def cmd_info(args):
    path = bookmarks_file(args)
    bm = Bookmarks(path)
    md5, _ = compute_checksums(bm.data)
    info = {
        "file": str(path),
        "browser": args.browser,
        "browser_running": browser_running(args.browser),
        "bookmarks": sum(1 for _ in bm.iter_urls()),
        "folders": sum(1 for _ in bm.iter_folders()) - len(bm.roots()),
        "roots": [{"id": r["id"], "name": r.get("name"), "bookmarks": sum(1 for _ in bm.iter_urls(r))} for r in bm.roots()],
        "checksum_valid": bm.data.get("checksum") in (None, md5),
        "sync_metadata_present": "sync_metadata" in bm.data,
        "backups": len(list_backups(path)),
        "backup_dir": str(backup_dir()),
    }
    if args.json:
        return out_json(info)
    for k, v in info.items():
        if k == "roots":
            for r in v:
                print(f"  root [{r['id']}] {r['name']}: {r['bookmarks']} bookmarks")
        else:
            print(f"{k:22} {v}")
    if info["sync_metadata_present"]:
        print("\nNote: Chrome Sync is (or was) enabled for this profile. See README > 'Chrome Sync'.")


def cmd_tree(args):
    bm = Bookmarks(bookmarks_file(args))
    starts = [bm.resolve(args.ref, want="folder")] if args.ref else bm.roots()
    if args.json:
        def build(n, depth):
            d = node_summary(bm, n)
            if n.get("type") == "folder" and (args.depth is None or depth < args.depth):
                d["children"] = [build(c, depth + 1) for c in n.get("children", [])
                                 if not args.folders_only or c.get("type") == "folder"]
            return d
        return out_json([build(s, 0) for s in starts])

    def show(n, depth):
        pad = "  " * depth
        if n.get("type") == "folder":
            cnt = sum(1 for _ in bm.iter_urls(n))
            print(f"{pad}+ {n.get('name')}  [{n['id']}] ({cnt})")
            if args.depth is None or depth < args.depth:
                for c in n.get("children", []):
                    show(c, depth + 1)
        elif not args.folders_only:
            url = f"  {n.get('url')}" if args.urls else ""
            print(f"{pad}- {n.get('name')}  [{n['id']}]{url}")

    for s in starts:
        show(s, 0)


def _filter_nodes(bm: Bookmarks, args):
    scope = bm.resolve(args.scope, want="folder") if getattr(args, "scope", None) else None
    nodes = list(bm.iter_urls(scope)) if not getattr(args, "folders", False) else \
        [f for f in bm.iter_folders(scope) if not bm.is_root(f)]
    q = getattr(args, "query", None)
    if q:
        if args.regex:
            rx = re.compile(q, re.I)
            match = lambda s: bool(rx.search(s))  # noqa: E731
        else:
            terms = q.lower().split()
            match = lambda s: all(t in s.lower() for t in terms)  # noqa: E731
        fields = args.field
        nodes = [n for n in nodes if match(" ".join(
            n.get(k, "") if k != "folder" else bm.folder_path_of(n) for k in fields))]
    if getattr(args, "domain", None):
        d = args.domain.lower().removeprefix("www.")
        nodes = [n for n in nodes if domain_of(n.get("url", "")) == d or domain_of(n.get("url", "")).endswith("." + d)]
    if getattr(args, "unused_days", None) is not None:
        cutoff = (time.time() + WEBKIT_EPOCH_OFFSET - args.unused_days * 86400) * 1_000_000
        nodes = [n for n in nodes if int(n.get("date_last_used", "0") or 0) < cutoff
                 and int(n.get("date_added", "0") or 0) < cutoff]
    return nodes


def cmd_list(args):
    bm = Bookmarks(bookmarks_file(args))
    nodes = _filter_nodes(bm, args)
    if args.json:
        return out_json([node_summary(bm, n) for n in nodes])
    for n in nodes:
        if n.get("type") == "folder":
            print(f"[{n['id']}] {bm.path_of(n)}/  ({sum(1 for _ in bm.iter_urls(n))})")
        else:
            print(f"[{n['id']}] {bm.folder_path_of(n)} :: {n.get('name')}  <{n.get('url')}>")
    print(f"\n{len(nodes)} result(s)", file=sys.stderr)


def cmd_stats(args):
    bm = Bookmarks(bookmarks_file(args))
    urls = list(bm.iter_urls())
    folders = [f for f in bm.iter_folders() if not bm.is_root(f)]
    domains: dict[str, int] = {}
    for n in urls:
        domains[domain_of(n["url"])] = domains.get(domain_of(n["url"]), 0) + 1
    dup_groups = _duplicate_groups(bm, None)
    empty = [f for f in folders if not any(True for _ in bm.iter_urls(f))]
    big = sorted(folders, key=lambda f: -len(f.get("children", [])))[:10]
    loose = {r.get("name"): sum(1 for c in r.get("children", []) if c.get("type") == "url") for r in bm.roots()}
    stats = {
        "bookmarks": len(urls),
        "folders": len(folders),
        "max_depth": max((len(bm.path_parts(f)) - 1 for f in folders), default=0),
        "unfiled_bookmarks_per_root": loose,
        "duplicate_groups": len(dup_groups),
        "duplicate_extra_copies": sum(len(g) - 1 for g in dup_groups),
        "empty_folders": [{"id": f["id"], "path": bm.path_of(f)} for f in empty],
        "non_http": sum(1 for n in urls if not n["url"].lower().startswith(("http://", "https://"))),
        "insecure_http": sum(1 for n in urls if n["url"].lower().startswith("http://")),
        "never_used": sum(1 for n in urls if (n.get("date_last_used") or "0") == "0"),
        "top_domains": sorted(domains.items(), key=lambda kv: -kv[1])[: args.top],
        "largest_folders": [{"id": f["id"], "path": bm.path_of(f), "items": len(f.get("children", []))} for f in big],
    }
    if args.json:
        return out_json(stats)
    for k, v in stats.items():
        if isinstance(v, list):
            print(f"{k}:")
            for item in v:
                print(f"   {item}")
        else:
            print(f"{k:28} {v}")


def cmd_export(args):
    bm = Bookmarks(bookmarks_file(args))
    scope = bm.resolve(args.scope, want="folder") if args.scope else None
    fmt = args.format
    if fmt == "json":
        payload = {
            "folders": [{"id": f["id"], "path": bm.path_of(f), "bookmarks": sum(1 for _ in bm.iter_urls(f))}
                        for f in bm.iter_folders(scope)],
            "bookmarks": [{k: v for k, v in node_summary(bm, n).items() if k != "type"} for n in bm.iter_urls(scope)],
        }
        text = json.dumps(payload, indent=1 if args.output else 2, ensure_ascii=False)
    elif fmt == "md":
        lines = []

        def md(n, depth):
            if n.get("type") == "folder":
                lines.append(f"{'#' * min(depth + 1, 6)} {n.get('name')} `[{n['id']}]`\n")
                urls = [c for c in n.get("children", []) if c.get("type") == "url"]
                for c in urls:
                    lines.append(f"- [{c.get('name')}]({c.get('url')}) `[{c['id']}]`")
                if urls:
                    lines.append("")
                for c in n.get("children", []):
                    if c.get("type") == "folder":
                        md(c, depth + 1)

        for s in ([scope] if scope else bm.roots()):
            md(s, 0)
        text = "\n".join(lines)
    else:  # netscape html (importable by every browser)
        lines = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>",
                 '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
                 "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks</H1>", "<DL><p>"]

        def unix(v):
            try:
                return int(int(v) / 1_000_000 - WEBKIT_EPOCH_OFFSET)
            except (TypeError, ValueError):
                return 0

        def h(n, depth):
            pad = "    " * depth
            if n.get("type") == "folder":
                lines.append(f'{pad}<DT><H3 ADD_DATE="{unix(n.get("date_added"))}">{html.escape(n.get("name", ""))}</H3>')
                lines.append(f"{pad}<DL><p>")
                for c in n.get("children", []):
                    h(c, depth + 1)
                lines.append(f"{pad}</DL><p>")
            else:
                lines.append(f'{pad}<DT><A HREF="{html.escape(n.get("url", ""))}" ADD_DATE="{unix(n.get("date_added"))}">'
                             f'{html.escape(n.get("name", ""))}</A>')

        for s in ([scope] if scope else bm.roots()):
            h(s, 1)
        lines.append("</DL><p>")
        text = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(text)


def cmd_add(args):
    bm = Bookmarks(bookmarks_file(args))
    folder = bm.ensure_folder(args.folder) if args.create else bm.resolve(args.folder, want="folder")
    existing = [n for n in bm.iter_urls() if normalize_for_dedupe(n["url"]) == normalize_for_dedupe(canonical_url(args.url))]
    if existing and not args.allow_duplicate:
        raise BmkError("Already bookmarked: " + "; ".join(f"[{n['id']}] {bm.path_of(n)}" for n in existing)
                       + "  (use --allow-duplicate to add anyway)")
    bm.add_url(folder, args.url, args.name, args.index)
    finish(bm, args)


def cmd_mkdir(args):
    bm = Bookmarks(bookmarks_file(args))
    for p in args.paths:
        bm.ensure_folder(p)
    finish(bm, args)


def cmd_rename(args):
    bm = Bookmarks(bookmarks_file(args))
    bm.rename(bm.resolve(args.ref), args.name)
    finish(bm, args)


def cmd_set_url(args):
    bm = Bookmarks(bookmarks_file(args))
    bm.set_url(bm.resolve(args.ref, want="url"), args.url)
    finish(bm, args)


def cmd_move(args):
    bm = Bookmarks(bookmarks_file(args))
    dest = bm.ensure_folder(args.to) if args.create else bm.resolve(args.to, want="folder")
    for i, ref in enumerate(args.refs):
        bm.move(bm.resolve(ref), dest, None if args.index is None else args.index + i)
    finish(bm, args)


def cmd_delete(args):
    bm = Bookmarks(bookmarks_file(args))
    for ref in args.refs:
        bm.delete(bm.resolve(ref))
    finish(bm, args, needs_yes=True, reason="delete")


def cmd_sort(args):
    bm = Bookmarks(bookmarks_file(args))
    bm.sort(bm.resolve(args.ref, want="folder"), args.by, not args.mixed, args.recursive)
    finish(bm, args)


def _duplicate_groups(bm: Bookmarks, scope, exact: bool = False) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for n in bm.iter_urls(scope):
        key = n["url"] if exact else normalize_for_dedupe(n["url"])
        groups.setdefault(key, []).append(n)
    return [g for g in groups.values() if len(g) > 1]


def cmd_dedupe(args):
    bm = Bookmarks(bookmarks_file(args))
    scope = bm.resolve(args.scope, want="folder") if args.scope else None
    groups = _duplicate_groups(bm, scope, args.exact)
    if args.json:
        return out_json([[node_summary(bm, n) for n in g] for g in groups])
    for g in groups:
        def rank(n):
            added = int(n.get("date_added", "0") or 0)
            used = int(n.get("date_last_used", "0") or 0)
            depth = len(bm.path_parts(n))
            return {"oldest": (added,), "newest": (-added,), "used": (-used, added),
                    "deepest": (-depth, added)}[args.keep]
        g.sort(key=rank)
        keep = g[0]
        print(f"keep   [{keep['id']}] {bm.path_of(keep)}  <{keep['url']}>")
        for n in g[1:]:
            bm.delete(n)
    finish(bm, args, needs_yes=True, reason="dedupe")


def cmd_prune(args):
    bm = Bookmarks(bookmarks_file(args))
    scope = bm.resolve(args.scope, want="folder") if args.scope else None
    changed = True
    while changed:  # repeat so nested empty folders collapse
        changed = False
        for f in [f for f in bm.iter_folders(scope) if not bm.is_root(f) and f is not scope]:
            if f["id"] in bm.nodes and not f.get("children"):
                bm.delete(f)
                changed = True
    finish(bm, args, needs_yes=True, reason="prune")


def cmd_check(args):
    bm = Bookmarks(bookmarks_file(args))
    scope = bm.resolve(args.scope, want="folder") if args.scope else None
    results = run_checks(bm, scope, args.timeout, args.workers)
    if args.only:
        results = [r for r in results if r["verdict"] in args.only]
    report = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "file": str(bm.path), "results": results}
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.json:
        return out_json(report)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        if r["verdict"] != "ok":
            extra = r["final_url"] if r["verdict"] == "redirect" else (r["error"] or r["status"])
            flag = (" PERMANENT" if r.get("permanent") else "") + (" SUSPICIOUS" if r.get("suspicious") else "")
            print(f"{r['verdict']:8} [{r['id']}] {r['folder']} :: {r['name']}\n         {r['url']}  -> {extra}{flag}")
    print("\nSummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.output:
        print(f"Report written to {args.output}")


def cmd_renew(args):
    """Update redirected URLs to their final location (and optionally http->https)."""
    bm = Bookmarks(bookmarks_file(args))
    if args.report:
        results = json.loads(Path(args.report).read_text(encoding="utf-8"))["results"]
    else:
        scope = bm.resolve(args.scope, want="folder") if args.scope else None
        results = run_checks(bm, scope, args.timeout, args.workers)
    for r in results:
        node = bm.nodes.get(r["id"])
        if not node or node.get("type") != "url" or node["url"] != r["url"]:
            continue
        if r["verdict"] == "redirect" and (r.get("permanent") or args.include_temporary) \
                and (not r.get("suspicious") or args.include_suspicious):
            bm.set_url(node, r["final_url"])
        elif args.https and r["verdict"] == "ok" and node["url"].startswith("http://"):
            https = "https://" + node["url"][len("http://"):]
            if check_url(https, args.timeout)["verdict"] in ("ok", "redirect"):
                bm.set_url(node, https)
    finish(bm, args, needs_yes=True, reason="renew")


def _rule_matches(rule: dict, node: dict) -> bool:
    url, name = node.get("url", ""), node.get("name", "")
    dom = domain_of(url)
    if any(dom == d.lower().removeprefix("www.") or dom.endswith("." + d.lower().removeprefix("www."))
           for d in rule.get("domains", [])):
        return True
    if rule.get("url_regex") and re.search(rule["url_regex"], url, re.I):
        return True
    if rule.get("name_regex") and re.search(rule["name_regex"], name, re.I):
        return True
    return False


def cmd_organize(args):
    bm = Bookmarks(bookmarks_file(args))
    cfg = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    rules = cfg["rules"] if isinstance(cfg, dict) else cfg
    scope_ref = args.scope or (cfg.get("scope") if isinstance(cfg, dict) else None)
    scope = bm.resolve(scope_ref, want="folder") if scope_ref else None
    for node in list(bm.iter_urls(scope)):
        for rule in rules:
            if _rule_matches(rule, node):
                target_path = rule["folder"]
                current = bm.folder_path_of(node)
                if current.lower() == target_path.strip("/").lower() or \
                        (not args.override and current.lower().startswith(target_path.strip("/").lower() + "/")):
                    break
                bm.move(node, bm.ensure_folder(target_path))
                break
    if args.sort:
        for rule in rules:
            try:
                bm.sort(bm.resolve(rule["folder"], want="folder"))
            except BmkError:
                pass
    finish(bm, args, needs_yes=True, reason="organize")


def apply_operations(bm: Bookmarks, ops: list[dict]):
    """Apply a plan in memory. Raises BmkError (nothing is saved) on the first bad op."""
    for i, op in enumerate(ops, 1):
        kind = op.get("op", "").replace("-", "_")
        ref = op.get("id") or op.get("ref")
        try:
            if kind == "mkdir":
                bm.ensure_folder(op["path"])
            elif kind == "add":
                bm.add_url(bm.ensure_folder(op["folder"]), op["url"], op.get("name"), op.get("index"))
            elif kind == "rename":
                bm.rename(bm.resolve(ref), op["name"])
            elif kind == "set_url":
                bm.set_url(bm.resolve(ref, want="url"), op["url"])
            elif kind == "move":
                to = str(op.get("to") or op.get("folder"))
                dest = bm.resolve(to, want="folder") if to.isdigit() or to.startswith("id:") else bm.ensure_folder(to)
                bm.move(bm.resolve(ref), dest, op.get("index"))
            elif kind == "delete":
                bm.delete(bm.resolve(ref))
            elif kind == "sort":
                bm.sort(bm.resolve(op.get("folder") or ref, want="folder"), op.get("by", "name"),
                        op.get("folders_first", True), op.get("recursive", False))
            elif kind == "prune_empty":
                scope = bm.resolve(op["scope"], want="folder") if op.get("scope") else None
                for f in sorted((f for f in bm.iter_folders(scope) if not bm.is_root(f) and f is not scope),
                                key=lambda f: -len(bm.path_parts(f))):
                    if f["id"] in bm.nodes and not f.get("children"):
                        bm.delete(f)
            else:
                raise BmkError(f"unknown op {op.get('op')!r}")
        except KeyError as e:
            raise BmkError(f"operation #{i} {op}: missing field {e}") from None
        except BmkError as e:
            raise BmkError(f"operation #{i} {op}: {e}") from None


def cmd_apply(args):
    bm = Bookmarks(bookmarks_file(args))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    ops = plan["operations"] if isinstance(plan, dict) else plan
    apply_operations(bm, ops)
    finish(bm, args, needs_yes=True, reason="apply")


def cmd_backup(args):
    path = bookmarks_file(args)
    if not path.exists():
        raise BmkError(f"Bookmarks file not found: {path}")
    print(make_backup(path, "manual"))


def cmd_backups(args):
    path = bookmarks_file(args)
    items = list_backups(path)
    if args.json:
        return out_json([str(p) for p in items])
    for p in items:
        print(p)
    if not items:
        print(f"(no backups in {backup_dir()})")


def cmd_restore(args):
    path = bookmarks_file(args)
    items = list_backups(path)
    src = Path(args.backup) if args.backup else (items[-1] if items else None)
    if not src or not src.exists():
        raise BmkError("No backup to restore")
    json.loads(src.read_text(encoding="utf-8"))  # validate
    print(f"restore {src}\n     -> {path}")
    if args.dry_run or not args.yes:
        print("\nPREVIEW ONLY - re-run with --yes to restore.")
        return
    if not args.force and browser_running(args.browser):
        raise BmkError(f"{args.browser} is running. Quit it completely and retry.")
    pre = make_backup(path, "pre-restore")
    shutil.copy2(src, path)
    print(f"Restored. Previous state saved as {pre}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--browser", choices=sorted(BROWSERS), default=os.environ.get("BMK_BROWSER", "chrome"),
                        help="browser (default: chrome, env BMK_BROWSER)")
    common.add_argument("--profile", default=os.environ.get("BMK_PROFILE", "Default"),
                        help="profile directory, e.g. 'Default', 'Profile 1' (env BMK_PROFILE)")
    common.add_argument("--file", help="explicit path to a Bookmarks file (env BMK_FILE)")
    common.add_argument("--json", action="store_true", help="machine-readable output where supported")

    write = argparse.ArgumentParser(add_help=False)
    write.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    write.add_argument("--force", action="store_true", help="skip the 'browser is running' check")

    confirm = argparse.ArgumentParser(add_help=False)
    confirm.add_argument("-y", "--yes", action="store_true", help="actually apply (otherwise preview only)")

    net = argparse.ArgumentParser(add_help=False)
    net.add_argument("--timeout", type=float, default=10.0)
    net.add_argument("--workers", type=int, default=16)

    p = argparse.ArgumentParser(prog="bookmarks.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name, fn, help_, parents=(), **kw):
        sp = sub.add_parser(name, help=help_, parents=[common, *parents], **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("profiles", cmd_profiles, "list browser profiles")
    add("info", cmd_info, "file location, counts, checksum and sync status")

    sp = add("tree", cmd_tree, "print the folder tree")
    sp.add_argument("ref", nargs="?", help="folder id or path (default: all roots)")
    sp.add_argument("--depth", type=int)
    sp.add_argument("--folders-only", action="store_true")
    sp.add_argument("--urls", action="store_true", help="show URLs")

    for name, help_ in (("list", "list bookmarks (or folders) with filters"), ("search", "search bookmarks")):
        sp = add(name, cmd_list, help_)
        sp.add_argument("query", nargs="?" if name == "list" else None, help="words (all must match) or --regex")
        sp.add_argument("--regex", action="store_true")
        sp.add_argument("--field", nargs="+", default=["name", "url"], choices=["name", "url", "folder"])
        sp.add_argument("--scope", help="limit to this folder")
        sp.add_argument("--domain", help="only this domain (and subdomains)")
        sp.add_argument("--unused-days", type=int, help="not opened (and not added) in N days")
        sp.add_argument("--folders", action="store_true", help="list folders instead of bookmarks")

    sp = add("stats", cmd_stats, "overview: duplicates, empty folders, top domains ...")
    sp.add_argument("--top", type=int, default=20)

    sp = add("export", cmd_export, "export as json (LLM-friendly), md, or html (Netscape)")
    sp.add_argument("--format", choices=["json", "md", "html"], default="json")
    sp.add_argument("--scope")
    sp.add_argument("-o", "--output")

    sp = add("add", cmd_add, "add a bookmark", [write])
    sp.add_argument("url")
    sp.add_argument("--name")
    sp.add_argument("--folder", default="other")
    sp.add_argument("--create", action="store_true", help="create the folder path if missing")
    sp.add_argument("--index", type=int)
    sp.add_argument("--allow-duplicate", action="store_true")

    sp = add("mkdir", cmd_mkdir, "create folder path(s) (like mkdir -p)", [write])
    sp.add_argument("paths", nargs="+")

    sp = add("rename", cmd_rename, "rename a bookmark or folder", [write])
    sp.add_argument("ref")
    sp.add_argument("name")

    sp = add("set-url", cmd_set_url, "change a bookmark's URL", [write])
    sp.add_argument("ref")
    sp.add_argument("url")

    sp = add("move", cmd_move, "move bookmarks/folders into a folder", [write])
    sp.add_argument("refs", nargs="+")
    sp.add_argument("--to", required=True)
    sp.add_argument("--create", action="store_true", help="create destination path if missing")
    sp.add_argument("--index", type=int)

    sp = add("delete", cmd_delete, "delete bookmarks/folders (folders recursively)", [write, confirm])
    sp.add_argument("refs", nargs="+")

    sp = add("sort", cmd_sort, "sort a folder", [write])
    sp.add_argument("ref")
    sp.add_argument("--by", choices=["name", "url", "date", "domain"], default="name")
    sp.add_argument("--mixed", action="store_true", help="don't put folders first")
    sp.add_argument("-r", "--recursive", action="store_true")

    sp = add("dedupe", cmd_dedupe, "remove duplicate URLs", [write, confirm])
    sp.add_argument("--scope")
    sp.add_argument("--keep", choices=["oldest", "newest", "used", "deepest"], default="deepest",
                    help="which copy survives (default: deepest = most organised)")
    sp.add_argument("--exact", action="store_true", help="exact URL match only (no normalisation)")

    sp = add("prune", cmd_prune, "delete empty folders", [write, confirm])
    sp.add_argument("--scope")

    sp = add("check", cmd_check, "check links: dead / redirect / blocked / error", [net])
    sp.add_argument("--scope")
    sp.add_argument("--only", nargs="+", choices=["ok", "redirect", "dead", "blocked", "error", "skipped"])
    sp.add_argument("-o", "--output", help="write JSON report (input for `renew --report`)")

    sp = add("renew", cmd_renew, "update redirected URLs to their new location", [write, confirm, net])
    sp.add_argument("--report", help="use a report from `check -o` instead of re-checking")
    sp.add_argument("--scope")
    sp.add_argument("--include-temporary", action="store_true", help="also follow 302/303/307 redirects")
    sp.add_argument("--include-suspicious", action="store_true", help="also redirects to login/home pages")
    sp.add_argument("--https", action="store_true", help="upgrade http:// links that work over https")

    sp = add("organize", cmd_organize, "move bookmarks into folders using a rules file", [write, confirm])
    sp.add_argument("rules", help="rules JSON, see examples/rules.example.json")
    sp.add_argument("--scope", help="only organise bookmarks under this folder")
    sp.add_argument("--override", action="store_true", help="also move bookmarks already in a subfolder of the target")
    sp.add_argument("--sort", action="store_true", help="sort target folders afterwards")

    sp = add("apply", cmd_apply, "apply a JSON plan of operations atomically", [write, confirm])
    sp.add_argument("plan", help="plan JSON, see examples/plan.example.json")

    add("backup", cmd_backup, "make a manual backup now")
    add("backups", cmd_backups, "list backups")
    sp = add("restore", cmd_restore, "restore a backup (latest by default)", [write, confirm])
    sp.add_argument("backup", nargs="?")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    if not hasattr(args, "force"):
        args.force = False
    try:
        args.fn(args)
        return 0
    except BmkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, re.error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
