#!/usr/bin/env python3
"""asset-seatbelt: a Claude Code hook that copies your assets before a tool call can wreck them.

When Claude drives Unreal (or any editor) through MCP tools, one call can move,
delete or overwrite live assets. This PreToolUse hook looks at every call; when
the tool's name says it writes (delete, move, rename, save, set, edit ...), or a
shell command removes or moves files, it copies the assets the call names into
.claude/seatbelt/snapshots/ first. If it cannot tell which assets and the whole
content folder is too big to copy, it asks you instead of guessing.
`seatbelt.py restore <id>` puts them back. Standard library only.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

__version__ = "0.2.2"

DEFAULTS = {
    "keep": 20,                 # snapshots to keep
    "max_full_mb": 1024,        # copy all content when targets are unknown, up to this size; else ask
    "watch": None,              # dirs to protect outside Unreal (default for Unreal: Content, Plugins/*/Content)
    "extra_verbs": [],          # more tool-name words that mean "this writes"
}
WRITE_VERBS = {
    "delete", "remove", "move", "rename", "destroy", "overwrite", "replace", "consolidate", "save", "write",
    "set", "edit", "modify", "update", "reimport", "import", "fixup", "merge", "clear", "reset", "purge",
    "drop", "truncate", "apply", "change", "duplicate",
}
SHELL_TOOLS = {"Bash", "PowerShell"}
SHELL_DESTRUCTIVE = re.compile(
    r"(?<![\w-])(rm|rmdir|del|erase|rd|mv|move|ren|rename|shred|truncate)(?![\w-])|"
    r"Remove-Item|Move-Item|Rename-Item|Clear-Content|Set-Content|Out-File|"
    r"\bgit\s+(clean|reset\s+--hard|checkout\s+--|restore|rm|mv)\b|\bfind\b.*-delete\b", re.I)
UNREAL_EXT = (".uasset", ".umap")
PATH_IN_TEXT = re.compile(r"(?:^|[\s\"',;(\[=])(/[^\s\"',;)\]]+)")


class SeatbeltError(Exception):
    pass


# ---------- config / project ----------

def load_config(root):
    cfg = dict(DEFAULTS)
    p = os.path.join(root, ".claude", "seatbelt.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError) as e:
            raise SeatbeltError(f".claude/seatbelt.json is unreadable ({e})")
    return cfg


def is_unreal(root):
    try:
        return any(f.endswith(".uproject") for f in os.listdir(root))
    except OSError:
        return False


def content_dirs(root, cfg):
    if cfg.get("watch"):
        return [os.path.join(root, d) for d in cfg["watch"]]
    if not is_unreal(root):
        return []
    dirs = [os.path.join(root, "Content")] + [os.path.join(p, "Content") for p in _plugins(root).values()]
    return [d for d in dirs if os.path.isdir(d)]


def _plugins(root):
    """{mount name: plugin dir} for every folder under Plugins/ that holds a .uplugin."""
    found = {}
    base = os.path.join(root, "Plugins")
    if os.path.isdir(base):
        for d, subdirs, files in os.walk(base):
            if any(f.endswith(".uplugin") for f in files):
                found[os.path.basename(d)] = d
                subdirs[:] = []   # plugins do not nest
    return found


def _rel(root, p):
    return os.path.relpath(p, root).replace(os.sep, "/")


def _files_under(path):
    if os.path.isfile(path):
        yield path
        return
    for d, subdirs, files in os.walk(path):
        subdirs[:] = [s for s in subdirs if s not in (".git", "__pycache__")]
        for f in files:
            yield os.path.join(d, f)


# ---------- what does this call touch ----------

def _words(name):
    base = name.split("__")[-1]
    base = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", base)
    return {w.lower() for w in re.split(r"[\s_\-.:/]+", base) if w}


def is_destructive(tool_name, tool_input, extra_verbs=()):
    if tool_name in SHELL_TOOLS:
        cmd = str((tool_input or {}).get("command", ""))
        # `git checkout -- x` slipped past the old pattern (no word boundary after "--")
        return bool(SHELL_DESTRUCTIVE.search(cmd) or GIT_DISCARD.search(cmd) or GIT_CLEAN.search(cmd))
    words = _words(tool_name)
    return bool(words & (WRITE_VERBS | {v.lower() for v in extra_verbs}))


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def _mount_dir(root, mount):
    if mount == "Game":
        return os.path.join(root, "Content")
    plugin = _plugins(root).get(mount)
    return os.path.join(plugin, "Content") if plugin else None


def _unreal_targets(root, path):
    """/Game/Props/Chair(.Chair) -> Content/Props/Chair.uasset (or the folder /Game/Props -> Content/Props)."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return []
    base = _mount_dir(root, parts[0])
    if not base:
        return []
    rest = parts[1:]
    if rest:
        name = rest[-1].split(":")[0]          # drop a sub-object
        if "." in name and not name.endswith(UNREAL_EXT):
            name = name.split(".")[0]          # Chair.Chair -> Chair
        rest[-1] = name
    target = os.path.join(base, *rest)
    if os.path.isdir(target):
        return list(_files_under(target))
    hits = [target + ext for ext in UNREAL_EXT if os.path.isfile(target + ext)]
    if os.path.isfile(target):
        hits.append(target)
    return hits


def _inside(root, p):
    real_root = os.path.realpath(root)
    try:
        p = os.path.realpath(p)
    except (OSError, ValueError):
        return None
    if os.path.exists(p) and p != real_root and os.path.commonpath([p, real_root]) == real_root:
        return p
    return None


def _tokens(s):
    """Candidate paths in a command: the plain split AND a quote-aware one. `rm "my art/model.bin"` was split at the
    space into two names that do not exist, so nothing was saved (external review, 25.10). shlex in non-POSIX mode
    keeps Windows back-slashes; its quotes are stripped here. A candidate only counts if it exists, so extra ones cost
    nothing."""
    import shlex
    # the whole string first: a Write/Edit file_path is one path, no quotes, and was split at its space (a second review)
    out = [s.strip()] + [t for t in re.split(r"[\s\"',;]+", s) if t]
    try:
        out += [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in shlex.split(s, posix=False)]
    except ValueError:                                  # an unclosed quote: take what is inside each quote pair
        out += [a or b for a, b in re.findall(r'"([^"]+)"|\'([^\']+)\'', s)]
        out += re.findall(r'["\']([^"\']+)$', s)        # ...and the text after the quote left open
    try:
        out += shlex.split(s, posix=True)               # bash escapes: rm my\ art/model.bin (a second review)
    except ValueError:
        pass
    return list(dict.fromkeys(t for t in out if t))


# any flags first (--login, -l), then the command flag: -c alone or glued at the end of a group (-lc, -ic), or the
# PowerShell words. bash -lc / zsh -ic / bash --login -c slipped through the first form (a second review)
NESTED_SHELL = re.compile(r"\b(eval|(bash|sh|zsh|dash|ksh|pwsh|powershell)(\.exe)?\s+(--?[\w-]+\s+)*"
                          r"(-[a-z]*c|-command|-encodedcommand)\b|cmd(\.exe)?\s+/[ck]\b|ssh\s|xargs\s+(bash|sh)\b)", re.I)
UNRESOLVED = re.compile(r"\$\{?\w+|\$\(|`|%\w+%|\$env:", re.I)   # a target named through a variable or a subshell


def asset_files(root, tool_input, watch=None):
    """Files under the project that the call's arguments point at (globs like art/*.png are expanded)."""
    found = set()
    for s in _strings(tool_input):
        for m in PATH_IN_TEXT.finditer(s):
            for f in _unreal_targets(root, m.group(1)):
                found.add(_rel(root, f))
        for tok in _tokens(s):
            tok = tok.strip("()[]")
            if not tok or tok.startswith("-") or len(tok) > 400:
                continue
            base = tok if os.path.isabs(tok) else os.path.join(root, tok)
            matches = glob.glob(base, recursive=True) if glob.has_magic(tok) else [base]
            for cand in matches:
                p = _inside(root, cand)
                if not p:
                    continue
                for f in _files_under(p):
                    rel = _rel(root, f)
                    if not rel.startswith(".claude/"):
                        found.add(rel)
    return sorted(found)


# only the forms that throw changes away: a branch switch (git checkout main, -b feature) is not one
GIT_DISCARD = re.compile(r"\bgit\s+(reset\s+--hard|checkout\s+(.*\s)?(-f|--force|--)(\s|$)|checkout\s+\.(\s|$)|"
                         r"restore\b(?!.*--staged\s*$)|stash\b(?!\s+(list|show)))")
GIT_CLEAN = re.compile(r"\bgit\s+clean\b([^;&|]*)")


def _git_lines(root, *args):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.splitlines() if r.returncode == 0 else []


def git_targets(root, command):
    """The files a git command destroys without naming them: uncommitted changes for reset --hard / checkout --
    / restore / stash, and what `git clean` would delete (its own dry run, with the same flags)."""
    found = set()
    if GIT_DISCARD.search(command):
        for line in _git_lines(root, "diff", "--name-only", "HEAD") + _git_lines(root, "diff", "--name-only", "--cached"):
            if line and os.path.isfile(os.path.join(root, line)):
                found.add(line.replace("\\", "/"))
    m = GIT_CLEAN.search(command)
    if m:
        flags = [f for f in m.group(1).split() if f.startswith("-") and f not in ("-f", "--force", "-n", "--dry-run", "-i")]
        flags = [f.replace("f", "") if re.fullmatch(r"-[a-zA-Z]+", f) else f for f in flags]
        for line in _git_lines(root, "clean", "-n", *[f for f in flags if f not in ("-", "")]):
            if line.startswith("Would remove "):
                p = _inside(root, os.path.join(root, line[len("Would remove "):].strip()))
                if p:
                    found.update(_rel(root, f) for f in _files_under(p))
    return sorted(f for f in found if not f.startswith(".claude/"))


# ---------- snapshots ----------

def _snap_root(root):
    return os.path.join(root, ".claude", "seatbelt", "snapshots")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def take_snapshot(root, files, tool, tool_input, keep=None):
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1e6) % 1000000:06d}"
    safe = re.sub(r"[^\w.-]+", "_", tool.split("__")[-1])[:40]
    sid = f"{stamp}-{safe}"
    path = os.path.join(_snap_root(root), sid)
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(_snap_root(root), f"{sid}-{n}")
    sid = os.path.basename(path)
    os.makedirs(os.path.join(path, "files"))
    manifest = {"id": sid, "tool": tool, "input": json.dumps(tool_input, ensure_ascii=False)[:4000],
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": []}
    for rel in files:
        src = os.path.join(root, rel)
        dst = os.path.join(path, "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        manifest["files"].append({"path": rel, "size": os.path.getsize(src), "sha256": _sha(dst)})
    with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    if keep:
        prune(root, keep)
    return sid


def list_snapshots(root):
    base = _snap_root(root)
    if not os.path.isdir(base):
        return []
    out = []
    for sid in sorted(os.listdir(base)):
        mp = os.path.join(base, sid, "manifest.json")
        if not os.path.exists(mp):
            continue
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        out.append({"id": sid, "path": os.path.join(base, sid), "tool": m["tool"], "created": m["created"],
                    "files": len(m["files"]), "bytes": sum(x["size"] for x in m["files"])})
    return out


def prune(root, keep):
    snaps = list_snapshots(root)
    for s in snaps[:max(0, len(snaps) - keep)]:
        shutil.rmtree(s["path"], ignore_errors=True)


def restore(root, sid, dry_run=False):
    path = os.path.join(_snap_root(root), sid)
    mp = os.path.join(path, "manifest.json")
    if not os.path.exists(mp):
        raise SeatbeltError(f"no snapshot {sid}")
    with open(mp, encoding="utf-8") as f:
        m = json.load(f)
    todo, overwrite = [], []
    for item in m["files"]:
        live = os.path.join(root, item["path"])
        if not os.path.exists(live):
            todo.append(item["path"])
        elif _sha(live) != item["sha256"]:
            todo.append(item["path"])
            overwrite.append(item["path"])
    if dry_run or not todo:
        return todo
    if overwrite:   # restoring is itself undoable
        take_snapshot(root, overwrite, "seatbelt-restore", {"restoring": sid})
    for rel in todo:
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(path, "files", rel), dst)
    return todo


# ---------- the hook ----------

def _ask(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": f"asset-seatbelt: {reason}"}}))
    return 0


def _log(root, entry):
    try:
        d = os.path.join(root, ".claude", "seatbelt")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_hook(stdin_text, project=None):
    try:
        event = json.loads(stdin_text)
        tool, tool_input = event["tool_name"], event.get("tool_input") or {}
    except (ValueError, KeyError, TypeError):
        print("asset-seatbelt: could not read the hook input; doing nothing", file=sys.stderr)
        return 0
    root = project or os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()
    if not is_destructive(tool, tool_input):
        try:
            extra = load_config(root).get("extra_verbs") or []
        except SeatbeltError:
            return 0
        if not extra or not is_destructive(tool, tool_input, extra):
            return 0
    try:
        cfg = load_config(root)
        files = asset_files(root, tool_input)
        if tool in SHELL_TOOLS:     # git commands that discard work name no files: ask git which ones
            files = sorted(set(files) | set(git_targets(root, str(tool_input.get("command", "")))))
        dirs = content_dirs(root, cfg)
        if dirs:   # only protect what lives in the content folders
            dirs_rel = [_rel(root, d) + "/" for d in dirs]
            files = [f for f in files if any(f.startswith(d) for d in dirs_rel)]
        how = "named"
        if not files:
            cmd = str(tool_input.get("command", "")) if tool in SHELL_TOOLS else ""
            # a $ inside single quotes is literal in bash and PowerShell: awk '{print $1}' asked for nothing (a second review)
            # - unless the quotes feed a nested shell, which expands it: bash -c 'rm $TARGET' (the same review, next pass)
            seen = cmd if NESTED_SHELL.search(cmd) else re.sub(r"'[^']*'", "''", cmd)
            if not dirs and UNRESOLVED.search(seen):
                # a destructive command whose target is a variable or a subshell: what it deletes cannot be known
                # here, so it is never waved through as «nothing we guard» (external review, 25.10)
                _log(root, {"tool": tool, "decision": "ask", "why": "unresolved target"})
                return _ask(f"`{tool}` deletes or overwrites something named through a variable or a subshell, so "
                            "I cannot tell which files and took no snapshot. Commit or back up first, or allow it if "
                            "you are sure.")
            if not dirs:
                return 0   # nothing we guard is involved
            everything = [_rel(root, f) for d in dirs for f in _files_under(d)]
            size = sum(os.path.getsize(os.path.join(root, f)) for f in everything)
            if size > cfg["max_full_mb"] * 1024 * 1024 or (cfg["max_full_mb"] == 0 and everything):
                _log(root, {"tool": tool, "decision": "ask", "content_mb": round(size / 1e6, 1)})
                return _ask(f"`{tool}` writes, I could not tell which assets it touches, and copying all "
                            f"content ({size / 1e6:.0f} MB) is over the {cfg['max_full_mb']} MB limit, so no "
                            f"snapshot was taken. Commit or back up first, or allow it if you are sure.")
            files, how = everything, "all content"
        if not files:
            return 0
        sid = take_snapshot(root, files, tool, tool_input, keep=cfg["keep"])
        _log(root, {"tool": tool, "snapshot": sid, "files": len(files), "how": how})
        print(f"asset-seatbelt: {len(files)} file(s) saved to snapshot {sid}", file=sys.stderr)
        return 0
    except Exception as e:  # a broken seatbelt must not wave a destructive call through
        return _ask(f"`{tool}` writes and the snapshot failed ({e}). Back up first, or allow it if you are sure.")


# ---------- install ----------

def install(root):
    claude = os.path.join(root, ".claude")
    hooks_dir = os.path.join(claude, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    shutil.copy2(os.path.abspath(__file__), os.path.join(hooks_dir, "seatbelt.py"))
    settings = os.path.join(claude, "settings.json")
    data = {}
    if os.path.exists(settings):
        with open(settings, encoding="utf-8") as f:
            data = json.load(f)
        shutil.copy2(settings, settings + ".bak")
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if any("seatbelt.py" in c.get("command", "") for h in pre for c in h.get("hooks", [])):
        return settings
    py = "python" if os.name == "nt" else "python3"
    pre.append({"matcher": "mcp__.*|^Bash$|^PowerShell$", "hooks": [
        {"type": "command", "command": f'{py} "${{CLAUDE_PROJECT_DIR}}/.claude/hooks/seatbelt.py" hook'}]})
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return settings


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="seatbelt", description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="project root (default: $CLAUDE_PROJECT_DIR or here)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="run as a Claude Code PreToolUse hook (reads the event on stdin)")
    sub.add_parser("install", help="copy into .claude/hooks and register in .claude/settings.json")
    sub.add_parser("list", help="list snapshots")
    r = sub.add_parser("restore", help="put a snapshot's files back")
    r.add_argument("id", help="snapshot id, or 'last'")
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = a.project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    if a.cmd == "hook":
        return run_hook(sys.stdin.read(), project=a.project)
    if a.cmd == "install":
        print(f"registered in {install(root)}; snapshots go to .claude/seatbelt/snapshots/")
        print("tip: add .claude/seatbelt/ to .gitignore")
        return 0
    if a.cmd == "list":
        snaps = list_snapshots(root)
        for s in snaps:
            print(f"{s['id']:<60} {s['files']:>5} files {s['bytes'] / 1e6:9.1f} MB  {s['tool']}")
        if not snaps:
            print("no snapshots")
        return 0
    if a.cmd == "restore":
        sid = a.id
        if sid == "last":
            snaps = [s for s in list_snapshots(root) if s["tool"] != "seatbelt-restore"]
            if not snaps:
                print("no snapshots", file=sys.stderr)
                return 2
            sid = snaps[-1]["id"]
        try:
            done = restore(root, sid, a.dry_run)
        except SeatbeltError as e:
            print(f"seatbelt: {e}", file=sys.stderr)
            return 2
        verb = "would restore" if a.dry_run else "restored"
        print(f"{verb} {len(done)} file(s) from {sid}")
        for f in done:
            print(f"  {f}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
