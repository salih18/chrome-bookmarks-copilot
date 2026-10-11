"""Run: python -m unittest discover -s tests -v"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "skills" / "chrome-bookmarks" / "scripts"))
import bookmarks as bmk  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "Bookmarks"


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "Default" / "Bookmarks"
        self.file.parent.mkdir()
        shutil.copy(FIXTURE, self.file)
        os.environ["BMK_BACKUP_DIR"] = str(self.tmp / "backups")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bmk.main([*argv, "--file", str(self.file)])
        self.assertEqual(code, expect, f"stdout={out.getvalue()}\nstderr={err.getvalue()}")
        return out.getvalue()

    def write(self, *argv, expect=0):
        return self.run_cli(*argv, "--force", expect=expect)

    def load(self):
        return bmk.Bookmarks(self.file)


class TestChecksum(unittest.TestCase):
    def test_fixture_checksum_matches(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        md5, _ = bmk.compute_checksums(data)
        self.assertEqual(md5, data["checksum"])

    def test_unicode_titles_use_utf16(self):
        data = {"roots": {"bookmark_bar": {"id": "1", "name": "ü😀", "type": "folder", "children": []}}}
        import hashlib
        expected = hashlib.md5(b"1" + "ü😀".encode("utf-16-le") + b"folder").hexdigest()
        self.assertEqual(bmk.compute_checksums(data)[0], expected)


class TestReadCommands(CliTestCase):
    def test_tree_and_paths(self):
        out = self.run_cli("tree")
        self.assertIn("+ Dev", out)
        self.assertIn("- GitHub", out)

    def test_resolve_aliases(self):
        bm = self.load()
        self.assertEqual(bm.resolve("bar/Dev")["name"], "Dev")
        self.assertEqual(bm.resolve("Bookmarks bar/dev")["name"], "Dev")
        self.assertEqual(bm.resolve("id:5")["name"], "GitHub")
        with self.assertRaises(bmk.BmkError):
            bm.resolve("bar/Nope")

    def test_search_json(self):
        res = json.loads(self.run_cli("search", "github", "--json"))
        self.assertEqual({r["id"] for r in res}, {"5", "11"})

    def test_stats(self):
        stats = json.loads(self.run_cli("stats", "--json"))
        self.assertEqual(stats["duplicate_groups"], 1)
        self.assertEqual([f["path"] for f in stats["empty_folders"]], ["Other bookmarks/Old", "Other bookmarks/Old/Empty"])

    def test_export_formats(self):
        data = json.loads(self.run_cli("export"))
        self.assertTrue(any(b["url"].startswith("https://github.com") for b in data["bookmarks"]))
        self.assertIn("<!DOCTYPE NETSCAPE-Bookmark-file-1>", self.run_cli("export", "--format", "html"))
        self.assertIn("# Bookmarks bar", self.run_cli("export", "--format", "md"))


class TestWriteCommands(CliTestCase):
    def assert_valid(self):
        data = json.loads(self.file.read_text(encoding="utf-8"))
        md5, sha = bmk.compute_checksums(data)
        self.assertEqual(data["checksum"], md5)
        self.assertEqual(data["checksum_sha256"], sha)
        ids = [n["id"] for n in bmk.Bookmarks(self.file).iter_nodes()]
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids")

    def test_add_mkdir_rename_move(self):
        self.write("add", "example.com/docs", "--name", "Docs", "--folder", "bar/Reading/Later", "--create")
        bm = self.load()
        node = bm.resolve("bar/Reading/Later/Docs")
        self.assertEqual(node["url"], "https://example.com/docs")
        self.write("rename", node["id"], "Example Docs")
        self.write("move", node["id"], "--to", "bar/Dev")
        self.assertEqual(self.load().resolve("bar/Dev/Example Docs")["id"], node["id"])
        self.assert_valid()
        self.assertTrue(list((self.tmp / "backups").glob("*.json")))

    def test_add_refuses_duplicate(self):
        self.write("add", "https://www.github.com/", "--folder", "other", expect=1)

    def test_delete_needs_yes(self):
        out = self.write("delete", "5")
        self.assertIn("PREVIEW ONLY", out)
        self.assertIn("5", self.load().nodes)
        self.write("delete", "5", "--yes")
        self.assertNotIn("5", self.load().nodes)
        self.assert_valid()

    def test_cannot_touch_roots_or_cycle(self):
        self.write("delete", "1", "--yes", expect=1)
        self.write("move", "4", "--to", "bar/Dev/Tools", expect=1)

    def test_dedupe_keeps_deepest(self):
        self.write("dedupe", "--yes")
        bm = self.load()
        self.assertIn("5", bm.nodes)       # bar/Dev/GitHub (deeper)
        self.assertNotIn("11", bm.nodes)   # other/github duplicate
        self.assert_valid()

    def test_prune_nested_empty(self):
        self.write("prune", "--yes")
        bm = self.load()
        self.assertNotIn("12", bm.nodes)
        self.assertNotIn("13", bm.nodes)
        self.assert_valid()

    def test_sort(self):
        self.write("sort", "bar/Dev")
        names = [c["name"] for c in self.load().resolve("bar/Dev")["children"]]
        self.assertEqual(names, ["Tools", "GitHub", "Python docs"])
        self.write("sort", "bar/Dev", "--by", "domain", "--mixed")
        self.write("sort", "bar", "--by", "url", "-r")

    def test_organize_rules(self):
        rules = self.tmp / "rules.json"
        rules.write_text(json.dumps({"rules": [{"folder": "bar/Video", "domains": ["youtube.com"]}]}))
        self.write("organize", str(rules), "--yes")
        self.assertEqual(self.load().resolve("bar/Video")["children"][0]["id"], "10")
        self.assert_valid()

    def test_apply_plan_atomic(self):
        good = self.tmp / "plan.json"
        good.write_text(json.dumps({"operations": [
            {"op": "mkdir", "path": "bar/News"},
            {"op": "move", "id": "9", "to": "bar/News"},
            {"op": "rename", "id": "9", "name": "HN"},
            {"op": "set_url", "id": "9", "url": "https://news.ycombinator.com/news"},
            {"op": "delete", "id": "11"},
            {"op": "prune_empty"},
        ]}))
        self.write("apply", str(good), "--yes")
        bm = self.load()
        self.assertEqual(bm.resolve("bar/News/HN")["url"], "https://news.ycombinator.com/news")
        self.assert_valid()

        before = self.file.read_bytes()
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps([{"op": "rename", "id": "5", "name": "x"}, {"op": "delete", "id": "999"}]))
        self.write("apply", str(bad), "--yes", expect=1)
        self.assertEqual(self.file.read_bytes(), before, "failed plan must not write anything")

    def test_restore(self):
        self.write("delete", "5", "--yes")
        self.write("restore", "--yes")
        self.assertIn("5", self.load().nodes)

    def test_dry_run_writes_nothing(self):
        before = self.file.read_bytes()
        self.write("rename", "5", "Nope", "--dry-run")
        self.assertEqual(self.file.read_bytes(), before)


class TestHelpers(unittest.TestCase):
    def test_normalize(self):
        n = bmk.normalize_for_dedupe
        self.assertEqual(n("https://www.Example.com/a/?utm_source=x#frag"), n("http://example.com/a"))
        self.assertNotEqual(n("https://example.com/a?id=1"), n("https://example.com/a?id=2"))

    def test_canonical(self):
        self.assertEqual(bmk.canonical_url("HTTPS://Example.COM"), "https://example.com/")
        self.assertEqual(bmk.canonical_url("javascript:alert(1)"), "javascript:alert(1)")

    def test_check_skips_non_http(self):
        self.assertEqual(bmk.check_url("chrome://settings")["verdict"], "skipped")


if __name__ == "__main__":
    unittest.main()
