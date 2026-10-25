"""Any project, not only Unreal: globs, and the git commands that destroy work without naming files
(git reset --hard, git checkout -- ., git restore, git clean). Red on 0.1.0."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_seatbelt import Project  # noqa: E402


class PlainProject(Project):
    """A web or Python project: no .uproject."""

    def __init__(self):
        super().__init__()
        os.remove(os.path.join(self.root, "Game.uproject"))
        self.write("art/a.png", b"png a")
        self.write("art/b.png", b"png b")
        self.write("notes.md", b"notes v1")

    def git(self, *a):
        return subprocess.run(["git", "-C", self.root, *a], capture_output=True, text=True, check=True).stdout


class AnyProject(unittest.TestCase):
    def setUp(self):
        self.p = PlainProject()

    def tearDown(self):
        self.p.tmp.cleanup()

    def test_a_glob_is_expanded(self):
        self.p.hook("Bash", {"command": "rm art/*.png"})
        snaps = self.p.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(self.p.files_in(snaps[0]), ["art/a.png", "art/b.png"])

    def test_git_reset_hard_saves_the_uncommitted_changes(self):
        p = self.p
        p.git("init", "-q")
        p.git("config", "user.email", "t@example.test")
        p.git("config", "user.name", "t")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "start")
        p.write("notes.md", b"notes v2, an afternoon of work")
        p.hook("Bash", {"command": "git reset --hard"})
        snaps = p.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(p.files_in(snaps[0]), ["notes.md"])
        with open(os.path.join(snaps[0]["path"], "files", "notes.md"), "rb") as f:
            self.assertEqual(f.read(), b"notes v2, an afternoon of work")

    def test_git_checkout_dot_and_restore_too(self):
        p = self.p
        p.git("init", "-q")
        p.git("config", "user.email", "t@example.test")
        p.git("config", "user.name", "t")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "start")
        p.write("notes.md", b"edited")
        for cmd in ("git checkout -- .", "git restore notes.md"):
            p.hook("PowerShell", {"command": cmd})
        self.assertEqual(len(p.snapshots()), 2)

    def test_git_clean_saves_what_it_would_delete(self):
        p = self.p
        p.git("init", "-q")
        p.git("config", "user.email", "t@example.test")
        p.git("config", "user.name", "t")
        p.git("add", "notes.md")
        p.git("commit", "-q", "-m", "start")
        p.hook("Bash", {"command": "git clean -fd"})
        snaps = p.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertIn("art/a.png", p.files_in(snaps[0]))
        self.assertNotIn("notes.md", p.files_in(snaps[0]))          # tracked: git clean does not touch it

    def test_a_branch_switch_is_not_a_discard(self):
        p = self.p
        p.git("init", "-q")
        p.git("config", "user.email", "t@example.test")
        p.git("config", "user.name", "t")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "start")
        p.write("notes.md", b"edited")
        for cmd in ("git checkout -b feature", "git checkout main", "git stash list"):
            p.hook("Bash", {"command": cmd})
        self.assertEqual(p.snapshots(), [])

    def test_a_clean_tree_needs_no_snapshot(self):
        p = self.p
        p.git("init", "-q")
        p.git("config", "user.email", "t@example.test")
        p.git("config", "user.name", "t")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "start")
        code, out = p.hook("Bash", {"command": "git reset --hard"})
        self.assertEqual((code, out), (0, None))
        self.assertEqual(p.snapshots(), [])


if __name__ == "__main__":
    unittest.main()
