"""An external review (25.10): `rm "my art/model.bin"` in a plain project took no snapshot and said nothing - the path
was split at the space. Red on 0.2.0."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_any_project import PlainProject  # noqa: E402


class QuotedPaths(unittest.TestCase):
    def setUp(self):
        self.p = PlainProject()
        self.p.write("my art/model.bin", b"an afternoon of sculpting")

    def tearDown(self):
        self.p.tmp.cleanup()

    def test_a_quoted_path_with_a_space_is_saved(self):
        self.p.hook("Bash", {"command": 'rm "my art/model.bin"'})
        snaps = self.p.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(self.p.files_in(snaps[0]), ["my art/model.bin"])

    def test_single_quotes_and_powershell_too(self):
        self.p.hook("PowerShell", {"command": "Remove-Item 'my art/model.bin'"})
        self.assertEqual(len(self.p.snapshots()), 1)

    def test_a_target_it_cannot_resolve_is_asked_about(self):
        code, out = self.p.hook("Bash", {"command": 'rm "$ASSET_DIR/model.bin"'})
        self.assertEqual(code, 0)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_write_and_edit_with_a_space_in_file_path_are_saved(self):
        # Tsaplya: a file_path is one path, no quotes - it was split at the space too
        self.p.hook("Write", {"file_path": os.path.join(self.p.root, "my art", "model.bin"), "content": "x"})
        self.assertEqual(len(self.p.snapshots()), 1)
        self.p.hook("Edit", {"file_path": "my art/model.bin", "old_string": "a", "new_string": "b"})
        self.assertEqual(len(self.p.snapshots()), 2)

    def test_a_backslash_escaped_space_is_read(self):
        self.p.hook("Bash", {"command": r"rm my\ art/model.bin"})
        self.assertEqual(len(self.p.snapshots()), 1)

    def test_a_dollar_inside_single_quotes_is_not_a_variable(self):
        code, out = self.p.hook("Bash", {"command": "rm nothere.bin | awk '{print $1}'"})
        self.assertIsNone(out)

    def test_single_quotes_fed_to_a_nested_shell_are_still_asked_about(self):
        # Tsaplya: blanking single quotes silenced bash -c 'rm $TARGET' - the inner shell expands it
        for cmd in ("bash -c 'rm $TARGET'", "sh -c 'rm \"$TARGET\"'", "pwsh -c 'Remove-Item $env:TARGET'",
                    "eval 'rm $X'"):
            with self.subTest(cmd=cmd):
                code, out = self.p.hook("Bash", {"command": cmd})
                self.assertIsNotNone(out, cmd)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_a_named_file_that_does_not_exist_is_not_asked_about(self):
        code, out = self.p.hook("Bash", {"command": "rm build/old.log"})          # nothing to lose
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
