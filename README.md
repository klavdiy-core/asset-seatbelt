# asset-seatbelt

A Claude Code hook that copies your assets before a tool call can wreck them.

> RU: Claude Code теперь управляет Unreal Engine и одним вызовом может переместить или удалить живые ассеты. Совет «коммить перед каждой длинной сессией» — это ремень, который надо не забыть пристегнуть. asset-seatbelt пристёгивает его сам: перед каждым пишущим вызовом копирует затронутые ассеты, а `restore` возвращает их.

## What hurts

With an MCP plugin, Claude edits a running Unreal Editor directly: it can **move or delete live assets in one call**, and the advice is to "commit before every long session". A big Unreal project is gigabytes of binary `.uasset` files, often in Perforce or in no VCS at all, and nobody commits before *each call*. One wrong `delete_assets` on a folder, or a `save` over the wrong blueprint, and the afternoon is gone.

## What it does

A `PreToolUse` hook. Before every MCP tool call (and every `Bash` / `PowerShell` command):

1. **Does it write?** It reads the tool name: `delete`, `remove`, `move`, `rename`, `save`, `set`, `edit`, `modify`, `update`, `replace`, `consolidate`, `reimport`… (snake_case, kebab-case and CamelCase all count). Read-only calls (`get`, `list`, `find`, `describe`…) pass untouched and cost nothing. Shell commands count when they `rm`, `del`, `mv`, `Remove-Item`, `git clean`, `git reset --hard` and the like.
2. **What does it touch?** It finds the assets named in the call's arguments: Unreal package paths (`/Game/Props/Chair`, object paths `/Game/Props/Chair.Chair`, folders `/Game/Props`, plugin mounts `/MyPlugin/...`) and plain file or folder paths inside the project. They map to `Content/…/*.uasset|.umap` and `Plugins/<Plugin>/Content/…`.
3. **Copies them first** into `.claude/seatbelt/snapshots/<time>-<tool>/` with a manifest (tool, arguments, SHA-256 per file). It keeps the last 20.
4. **If it cannot tell which assets** a writing call touches, it copies the whole content folder, as long as that is under 1 GB. Over the limit it does not guess: it **asks you** (`permissionDecision: "ask"`) and says why.
5. **If the seatbelt itself breaks** (unreadable config, disk error), it asks too. A broken seatbelt never waves a destructive call through.

Then:

```sh
python .claude/hooks/seatbelt.py list
python .claude/hooks/seatbelt.py restore last --dry-run
python .claude/hooks/seatbelt.py restore 20261024-174352-868853-delete_assets
```

`restore` puts back files that are missing or changed. Before it overwrites anything it snapshots what is there now, so a restore can be undone too.

Standard library only, Python 3.8+. Windows, macOS, Linux.

## Not only Unreal

In any project (no `.uproject`), it guards what a shell command destroys:

- files and folders named in `rm`, `del`, `mv`, `Remove-Item`… inside the project, **globs included** (`rm art/*.png`, `rm -rf build/**`);
- **`git reset --hard`, `git checkout -- …` / `git checkout .`, `git restore`, `git stash`**: they name no files, so it asks git which ones have uncommitted changes and copies those first;
- **`git clean -fd`** (and `-x`): it runs git's own dry run with the same flags and copies what would be deleted.

A branch switch (`git checkout main`, `git checkout -b x`) and `git stash list` are left alone. A clean tree takes no snapshot. `"watch": ["assets", "data"]` in `.claude/seatbelt.json` limits it to those folders.

## Install

From your Unreal project root (the folder with the `.uproject`):

```sh
curl -O https://raw.githubusercontent.com/klavdiy-core/asset-seatbelt/main/seatbelt.py
python seatbelt.py install
echo ".claude/seatbelt/" >> .gitignore
```

`install` copies the script to `.claude/hooks/seatbelt.py` and adds this to `.claude/settings.json`, keeping everything else there and leaving a `.bak` copy. Running it twice does nothing the second time.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__.*|^Bash$|^PowerShell$",
        "hooks": [{ "type": "command", "command": "python \"${CLAUDE_PROJECT_DIR}/.claude/hooks/seatbelt.py\" hook" }]
      }
    ]
  }
}
```

Optional `.claude/seatbelt.json`:

```json
{ "keep": 20, "max_full_mb": 1024, "extra_verbs": ["bake", "compile"], "watch": null }
```

Not an Unreal project? Set `"watch": ["assets", "levels"]` and it guards those folders the same way.

## Limits (said plainly)

- **It copies files on disk.** Unreal also holds assets in memory. After a restore, close the editor or reload the assets before you save anything, or the editor may write its in-memory version back over the restored files.
- A delete in Unreal can also rewrite *other* assets that referenced the deleted one (redirectors, fix-ups). Those are only in the snapshot if the call named them or the whole content folder was copied. For big refactors, keep committing to your VCS; this is a seatbelt, not version control.
- Paths in quotes, with spaces, are read whole (`rm "my art/model.bin"`, `Remove-Item 'my art/x.bin'`). A target named
  through a variable or a subshell (`$DIR/x`, `$(...)`, `%VAR%`) cannot be known before it runs: the call is asked
  about, never waved through. Before 0.2.1 the quoted path was split at the space and the delete went ahead with no
  snapshot and no word — found by an external review, fixed in <commit>.
- It recognises writes by the tool's **name**. A tool called `do_it` that deletes things is invisible to it; add its words via `extra_verbs`.
- Not yet tested against Epic's plugin itself (tool names may differ); tested against Unreal-style projects (Content, plugin mounts, `.uasset`/`.umap`, object paths) and real Claude Code hook input. If your plugin's tool names slip through, an issue with the name helps.

## Test

```sh
python -m unittest discover -s tests
```

A fake Unreal project (Content, a plugin with `.uplugin`, a level): write-verb detection in all naming styles, shell commands, package and object paths, folder deletes, all-content fallback, "ask" over the limit, "ask" on a broken config, garbage input, pruning to `keep`, non-Unreal `watch` dirs, restore (including the undo snapshot), dry runs, and an `install` that merges into an existing settings file twice without duplicating.

## License

MIT
