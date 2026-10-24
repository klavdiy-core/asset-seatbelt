import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import seatbelt  # noqa: E402

FILES = {
    "Content/Props/Chair.uasset": b"chair v1",
    "Content/Props/Table.uasset": b"table v1",
    "Content/Maps/Level01.umap": b"level v1",
    "Plugins/MyPlug/MyPlug.uplugin": b"{}",
    "Plugins/MyPlug/Content/Thing.uasset": b"thing v1",
    "Source/Game/Game.cpp": b"// code",
}


class Project:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        with open(os.path.join(self.root, "Game.uproject"), "w") as f:
            f.write("{}")
        for rel, data in FILES.items():
            self.write(rel, data)

    def write(self, rel, data):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def read(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

    def hook(self, tool, tool_input, config=None):
        if config:
            os.makedirs(os.path.join(self.root, ".claude"), exist_ok=True)
            with open(os.path.join(self.root, ".claude", "seatbelt.json"), "w") as f:
                json.dump(config, f)
        event = {"hook_event_name": "PreToolUse", "cwd": self.root, "tool_name": tool, "tool_input": tool_input}
        out = io.StringIO()
        with redirect_stdout(out):
            code = seatbelt.run_hook(json.dumps(event), project=self.root)
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def snapshots(self):
        return seatbelt.list_snapshots(self.root)

    def files_in(self, snap):
        base = os.path.join(snap["path"], "files")
        got = []
        for d, _, fs in os.walk(base):
            for f in fs:
                got.append(os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/"))
        return sorted(got)


class Detect(unittest.TestCase):
    def test_writing_verbs_in_the_tool_name(self):
        for name in ("mcp__unreal__delete_asset", "mcp__unreal__MoveAssets", "mcp__ue__rename-asset",
                     "mcp__unreal__save_asset", "mcp__unreal__set_property", "mcp__unreal__consolidate_assets"):
            self.assertTrue(seatbelt.is_destructive(name, {}), name)
        for name in ("mcp__unreal__get_asset", "mcp__unreal__list_assets", "mcp__unreal__find_references",
                     "Read", "Grep", "mcp__unreal__describe_actor"):
            self.assertFalse(seatbelt.is_destructive(name, {}), name)

    def test_bash(self):
        self.assertTrue(seatbelt.is_destructive("Bash", {"command": "rm -rf Content/Props"}))
        self.assertTrue(seatbelt.is_destructive("Bash", {"command": "git clean -fdx"}))
        self.assertTrue(seatbelt.is_destructive("PowerShell", {"command": "Remove-Item -Recurse Content\\Maps"}))
        self.assertFalse(seatbelt.is_destructive("Bash", {"command": "ls Content"}))

    def test_asset_paths(self):
        p = Project()
        got = seatbelt.asset_files(p.root, {"assets": ["/Game/Props/Chair.Chair", "/MyPlug/Thing"],
                                            "level": "/Game/Maps/Level01"})
        self.assertEqual(sorted(got), ["Content/Maps/Level01.umap", "Content/Props/Chair.uasset",
                                       "Plugins/MyPlug/Content/Thing.uasset"])


class Hook(unittest.TestCase):
    def test_delete_snapshots_just_that_asset(self):
        p = Project()
        code, out = p.hook("mcp__unreal__delete_asset", {"asset_path": "/Game/Props/Chair"})
        self.assertEqual(code, 0)
        self.assertIsNone(out)                       # no decision: the call goes on, now with a seatbelt
        snaps = p.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(p.files_in(snaps[0]), ["Content/Props/Chair.uasset"])

    def test_folder_delete_takes_the_folder(self):
        p = Project()
        p.hook("mcp__unreal__delete_folder", {"path": "/Game/Props"})
        self.assertEqual(p.files_in(p.snapshots()[0]), ["Content/Props/Chair.uasset", "Content/Props/Table.uasset"])

    def test_read_only_call_takes_nothing(self):
        p = Project()
        code, out = p.hook("mcp__unreal__get_asset", {"asset_path": "/Game/Props/Chair"})
        self.assertEqual((code, out), (0, None))
        self.assertEqual(p.snapshots(), [])

    def test_unknown_target_takes_all_content(self):
        p = Project()
        p.hook("mcp__unreal__delete_selected", {})
        self.assertEqual(p.files_in(p.snapshots()[0]), sorted(
            ["Content/Maps/Level01.umap", "Content/Props/Chair.uasset", "Content/Props/Table.uasset",
             "Plugins/MyPlug/Content/Thing.uasset"]))

    def test_unknown_target_over_the_limit_asks(self):
        p = Project()
        code, out = p.hook("mcp__unreal__delete_selected", {}, config={"max_full_mb": 0})
        self.assertEqual(code, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("snapshot", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(p.snapshots(), [])

    def test_bad_input_on_a_destructive_call_asks_instead_of_waving_it_through(self):
        p = Project()
        os.makedirs(os.path.join(p.root, ".claude"))
        with open(os.path.join(p.root, ".claude", "seatbelt.json"), "w") as f:
            f.write("{ not json")
        code, out = p.hook("mcp__unreal__delete_asset", {"asset_path": "/Game/Props/Chair"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_garbage_stdin_is_not_a_crash(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(seatbelt.run_hook("not json", project=tempfile.gettempdir()), 0)

    def test_keeps_the_last_n(self):
        p = Project()
        for i in range(5):
            p.hook("mcp__unreal__save_asset", {"asset_path": "/Game/Props/Chair"}, config={"keep": 3})
        self.assertEqual(len(p.snapshots()), 3)

    def test_not_an_unreal_project_uses_watch_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "assets"))
            with open(os.path.join(root, "assets", "a.png"), "wb") as f:
                f.write(b"png")
            os.makedirs(os.path.join(root, ".claude"))
            with open(os.path.join(root, ".claude", "seatbelt.json"), "w") as f:
                json.dump({"watch": ["assets"]}, f)
            event = {"tool_name": "Bash", "tool_input": {"command": "rm -rf assets"}, "cwd": root}
            with redirect_stdout(io.StringIO()):
                seatbelt.run_hook(json.dumps(event), project=root)
            self.assertEqual(len(seatbelt.list_snapshots(root)), 1)


class Restore(unittest.TestCase):
    def test_restore_brings_back_and_is_itself_undoable(self):
        p = Project()
        p.hook("mcp__unreal__delete_folder", {"path": "/Game/Props"})
        snap = p.snapshots()[0]
        os.remove(os.path.join(p.root, "Content/Props/Chair.uasset"))     # the call deleted it
        p.write("Content/Props/Table.uasset", b"table v2 (broken)")        # and mangled this one
        restored = seatbelt.restore(p.root, snap["id"])
        self.assertEqual(sorted(restored), ["Content/Props/Chair.uasset", "Content/Props/Table.uasset"])
        self.assertEqual(p.read("Content/Props/Chair.uasset"), b"chair v1")
        self.assertEqual(p.read("Content/Props/Table.uasset"), b"table v1")
        # what it overwrote was saved first
        pre = [s for s in p.snapshots() if s["tool"] == "seatbelt-restore"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(p.files_in(pre[0]), ["Content/Props/Table.uasset"])

    def test_dry_run_changes_nothing(self):
        p = Project()
        p.hook("mcp__unreal__delete_asset", {"asset_path": "/Game/Props/Chair"})
        os.remove(os.path.join(p.root, "Content/Props/Chair.uasset"))
        got = seatbelt.restore(p.root, p.snapshots()[0]["id"], dry_run=True)
        self.assertEqual(got, ["Content/Props/Chair.uasset"])
        self.assertFalse(os.path.exists(os.path.join(p.root, "Content/Props/Chair.uasset")))


class Install(unittest.TestCase):
    def test_install_merges_into_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".claude"))
            settings = os.path.join(root, ".claude", "settings.json")
            with open(settings, "w") as f:
                json.dump({"permissions": {"allow": ["Read"]},
                           "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                               {"type": "command", "command": "other.sh"}]}]}}, f)
            seatbelt.install(root)
            seatbelt.install(root)
            with open(settings) as f:
                s = json.load(f)
            self.assertEqual(s["permissions"], {"allow": ["Read"]})
            pre = s["hooks"]["PreToolUse"]
            ours = [h for h in pre for c in h["hooks"] if "seatbelt" in c["command"]]
            self.assertEqual(len(ours), 1)
            self.assertTrue(any(c["command"] == "other.sh" for h in pre for c in h["hooks"]))
            self.assertTrue(os.path.exists(settings + ".bak"))


if __name__ == "__main__":
    unittest.main()
